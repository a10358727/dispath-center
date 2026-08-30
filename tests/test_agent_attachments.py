"""P2-3: closed vocabulary and hard caps for Studio image attachments."""

import base64

import pytest

from app.agent_attachments import MAX_ATTACHMENT_BYTES, InvalidAttachmentError, validate_attachments


def test_validate_attachments_accepts_images_and_reports_size():
    cleaned = validate_attachments([{"type": "image", "media_type": "image/png", "data_base64": base64.b64encode(b"hello").decode()}])
    assert cleaned[0]["bytes"] == 5 and cleaned[0]["media_type"] == "image/png"
    assert validate_attachments(None) == [] and validate_attachments([]) == []


@pytest.mark.parametrize(
    "raw",
    [
        [{"type": "file", "media_type": "image/png", "data_base64": "aGk="}],
        [{"type": "image", "media_type": "image/bmp", "data_base64": "aGk="}],
        [{"type": "image", "media_type": "image/png", "data_base64": "not-base64!!"}],
        [{"type": "image", "media_type": "image/png", "data_base64": ""}],
        [{"type": "image", "media_type": "image/png", "data_base64": base64.b64encode(b"x" * (MAX_ATTACHMENT_BYTES + 1)).decode()}],
        [{"type": "image", "media_type": "image/png", "data_base64": "aGk="}] * 5,
    ],
)
def test_validate_attachments_rejects_everything_else(raw):
    with pytest.raises(InvalidAttachmentError):
        validate_attachments(raw)
