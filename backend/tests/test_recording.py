"""WAV-запись вызова: формат, микширование и защищённая выдача."""

import os
import wave
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
from fastapi.testclient import TestClient

from app.api.auth import Principal
from app.api.http import sessions
from app.domain.roles import Role
from app.main import app
from app.voice.recording import CallRecorder


def test_recorder_mixes_16_and_24_khz_into_atomic_wav(tmp_path):
    ticks = iter((10.0, 10.0, 10.02))
    path = tmp_path / "call.wav"
    recorder = CallRecorder(path, clock=lambda: next(ticks))
    recorder.add_pcm(np.full(320, 1000, dtype="<i2").tobytes(), sample_rate=16_000)
    recorder.add_pcm(np.full(480, 2000, dtype="<i2").tobytes(), sample_rate=24_000)

    assert recorder.finalize() == path
    assert recorder.finalize() == path
    assert not path.with_suffix(".wav.tmp").exists()
    assert not path.with_suffix(".wav.journal").exists()
    if os.name == "posix":  # Windows exposes a different permission model.
        assert os.stat(path).st_mode & 0o777 == 0o600
    with wave.open(str(path), "rb") as source:
        assert source.getnchannels() == 1
        assert source.getsampwidth() == 2
        assert source.getframerate() == 16_000
        assert source.getnframes() >= 640
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
    assert samples.max() >= 2000


def test_recorder_recovers_audio_journal_after_process_restart(tmp_path):
    path = tmp_path / "interrupted.wav"
    clock_value = [10.0]
    clock = lambda: clock_value[0]
    first_process = CallRecorder(path, clock=clock)
    first_process.add_pcm((1000).to_bytes(2, "little", signed=True) * 320, sample_rate=16_000)

    journal = path.with_suffix(".wav.journal")
    # Simulate power loss halfway through a journal record. The next process
    # must keep all complete audio and discard only the torn tail.
    first_process._journal.close()
    with journal.open("ab") as partial:
        partial.write(b"\x40\x01\x00\x00\x00\x00\x00\x00\x40\x01\x00\x00\x02\x00")

    clock_value[0] = 50.0  # monotonic origin changed across host restart
    recovered = CallRecorder(path, clock=clock)
    recovered.add_pcm((2000).to_bytes(2, "little", signed=True) * 320, sample_rate=16_000)
    recovered.finalize()

    with wave.open(str(path), "rb") as source:
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
        assert source.getframerate() == 16_000
    assert samples.size == 640
    assert np.all(samples[:320] == 1000)
    assert np.all(samples[320:] == 2000)
    assert not journal.exists()


def test_recording_download_is_authenticated_and_has_wav_type(tmp_path, monkeypatch):
    session_id = uuid4()
    path = tmp_path / f"{session_id}.wav"
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16_000)
        target.writeframes(b"\x00\x00" * 320)

    async def fake_session(db, requested):
        assert requested == session_id
        return SimpleNamespace(
            trainee_id=uuid4(), owner_login="dev", ended_at=datetime.now(UTC),
        )

    monkeypatch.setattr(sessions.repo, "get_session", fake_session)
    monkeypatch.setattr(sessions, "recording_path", lambda requested: path)
    audit_events = []

    async def record_access(actor, role, action, object_id=None, detail=""):
        audit_events.append((actor, role, action, object_id, detail))

    monkeypatch.setattr(sessions, "audit_required", record_access)
    with TestClient(app) as client:
        assert client.get(f"/api/sessions/{session_id}/recording.wav").status_code == 401
        client.post("/api/auth/dev-token")
        response = client.get(f"/api/sessions/{session_id}/recording.wav")
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/wav"
        assert response.content.startswith(b"RIFF")
        assert audit_events == [
            ("dev", "instructor", "recording.read", str(session_id), "")
        ]

        monkeypatch.setattr(
            sessions,
            "require",
            lambda request, *roles: Principal(
                login="other", full_name="Другой", role=Role.TRAINEE, trainee_id=uuid4()
            ),
        )
        assert client.get(f"/api/sessions/{session_id}/recording.wav").status_code == 403
        assert len(audit_events) == 1


def test_recording_is_not_returned_when_access_audit_is_unavailable(tmp_path, monkeypatch):
    from fastapi import HTTPException

    session_id = uuid4()
    path = tmp_path / f"{session_id}.wav"
    path.write_bytes(b"not returned")

    async def fake_session(db, requested):
        return SimpleNamespace(
            trainee_id=uuid4(), owner_login="dev", ended_at=datetime.now(UTC),
        )

    async def audit_unavailable(*_args, **_kwargs):
        raise HTTPException(status_code=503, detail="audit_unavailable")

    monkeypatch.setattr(sessions.repo, "get_session", fake_session)
    monkeypatch.setattr(sessions, "recording_path", lambda requested: path)
    monkeypatch.setattr(sessions, "audit_required", audit_unavailable)
    with TestClient(app) as client:
        client.post("/api/auth/dev-token")
        response = client.get(f"/api/sessions/{session_id}/recording.wav")
    assert response.status_code == 503
    assert response.json() == {"detail": "audit_unavailable"}
