"""WAV-запись вызова: формат, микширование и защищённая выдача."""

import wave
from datetime import datetime, timezone
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
    with wave.open(str(path), "rb") as source:
        assert source.getnchannels() == 1
        assert source.getsampwidth() == 2
        assert source.getframerate() == 16_000
        assert source.getnframes() >= 640
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
    assert samples.max() >= 2000


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
            trainee_id=uuid4(), owner_login="dev", ended_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(sessions.repo, "get_session", fake_session)
    monkeypatch.setattr(sessions, "recording_path", lambda requested: path)
    with TestClient(app) as client:
        assert client.get(f"/api/sessions/{session_id}/recording.wav").status_code == 401
        client.post("/api/auth/dev-token")
        response = client.get(f"/api/sessions/{session_id}/recording.wav")
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/wav"
        assert response.content.startswith(b"RIFF")

        monkeypatch.setattr(
            sessions,
            "require",
            lambda request, *roles: Principal(
                login="other", full_name="Другой", role=Role.TRAINEE, trainee_id=uuid4()
            ),
        )
        assert client.get(f"/api/sessions/{session_id}/recording.wav").status_code == 403
