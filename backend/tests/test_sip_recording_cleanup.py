import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from sip_recording_cleanup import remove_smoke_recordings  # noqa: E402


def test_cleanup_removes_only_new_wav_basenames_from_compose_volume(tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="")

    assert remove_smoke_recordings(
        tmp_path,
        {"20260926-120000-6101-6102-abc.wav", "../keep.wav", "old.txt"},
        runner=run,
    )

    command, options = calls[0]
    assert command[-4:] == [
        "rm", "-f", "--", "/recordings/20260926-120000-6101-6102-abc.wav",
    ]
    assert "../keep.wav" not in command
    assert "old.txt" not in command
    assert options["cwd"] == tmp_path
    assert len(calls) == 2
    assert calls[1][0][-8:] == [
        "find", "/recordings", "-maxdepth", "1", "-type", "f", "-name", "*.wav",
    ]


def test_cleanup_does_not_run_compose_for_empty_or_unsafe_names(tmp_path):
    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("no deletion command should be needed")

    assert remove_smoke_recordings(tmp_path, {"../outside.wav", "not-a-recording.txt"}, runner=unexpected_run)


def test_cleanup_reports_compose_failure(tmp_path):
    def fail(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1)

    assert not remove_smoke_recordings(tmp_path, {"new.wav"}, runner=fail)


def test_cleanup_fails_if_exact_smoke_file_still_exists(tmp_path):
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        output = "/recordings/new.wav\n" if len(calls) == 2 else ""
        return subprocess.CompletedProcess(command, 0, stdout=output)

    assert not remove_smoke_recordings(tmp_path, {"new.wav"}, runner=run)
