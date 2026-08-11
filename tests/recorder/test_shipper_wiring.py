"""Recorder.__init__ only starts the background shipper thread once this
device has enrollment state on disk; an unenrolled device just records
locally, same as before Phase 1.
"""

from __future__ import annotations

from dataclasses import asdict

from byoai.recorder.enroll import ENROLLMENT_FILENAME, EnrollmentState
from byoai.recorder.integration import Recorder


def test_no_shipper_without_enrollment(tmp_path):
    recorder = Recorder(dir=tmp_path)
    try:
        assert recorder._shipper is None
        assert recorder._shipper_thread is None
    finally:
        recorder.close()


def test_shipper_starts_when_enrolled(tmp_path):
    # Enroll manually (no network) by writing enrollment.json the same way
    # enroll() would, using this recorder's own device_id once it exists.
    recorder = Recorder(dir=tmp_path)
    device_id = recorder.key.device_id
    recorder.close()

    import json

    state = EnrollmentState(
        device_id=device_id,
        coriqo_base_url="https://coriqo.example.com",
        enrolled_at="2026-01-01T00:00:00Z",
    )
    (tmp_path / ENROLLMENT_FILENAME).write_text(json.dumps(asdict(state)))

    recorder2 = Recorder(dir=tmp_path)
    try:
        assert recorder2._shipper is not None
        assert recorder2._shipper_thread is not None
        assert recorder2._shipper_thread.is_alive()
    finally:
        recorder2.close()
    assert not recorder2._shipper_thread.is_alive()
