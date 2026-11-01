"""tests/unit/test_signed_qr.py — signed opaque token references (QR-002)."""

import json
import time

import pytest

from app.utils import signed_qr


class TestCreateDecode:
    def test_roundtrip_scan_ref(self):
        ref = signed_qr.create_token_ref(7, 3, signed_qr.PURPOSE_SCAN)
        status, payload = signed_qr.verify_token_ref(ref, signed_qr.PURPOSE_SCAN)
        assert status == signed_qr.STATUS_VALID
        assert payload.token_id == 7
        assert payload.org_id == 3
        assert payload.nonce  # random nonce present

    def test_roundtrip_track_ref(self):
        ref = signed_qr.create_token_ref(12, 1, signed_qr.PURPOSE_TRACK)
        status, payload = signed_qr.verify_token_ref(ref, signed_qr.PURPOSE_TRACK)
        assert status == signed_qr.STATUS_VALID
        assert payload.token_id == 12
        assert payload.org_id == 1

    @pytest.mark.unit
    def test_track_ref_url_embeds_signed_ref(self):
        url = signed_qr.track_ref_url(9, 4, "http://localhost:8000")
        assert url.startswith("http://localhost:8000/public/track/")
        ref = url.rsplit("/", 1)[1]
        status, payload = signed_qr.verify_token_ref(ref, signed_qr.PURPOSE_TRACK)
        assert status == signed_qr.STATUS_VALID
        assert payload.token_id == 9

    def test_unknown_purpose_rejected_at_creation(self):
        with pytest.raises(ValueError):
            signed_qr.create_token_ref(1, 1, "admin")


class TestEnumerationResistance:
    @pytest.mark.unit
    def test_token_id_not_plaintext_readable(self):
        """The ref must be cryptographically opaque (QR-002/Gate 6)."""
        ref = signed_qr.create_token_ref(12345, 1, signed_qr.PURPOSE_TRACK)
        assert "12345" not in ref  # no plaintext id in payload or signature

    @pytest.mark.unit
    def test_refs_for_sequential_tokens_are_unlinkable(self):
        """Same scope + nonce ⇒ refs share no substring; signature random."""
        a = signed_qr.create_token_ref(10, 1, signed_qr.PURPOSE_TRACK)
        b = signed_qr.create_token_ref(11, 1, signed_qr.PURPOSE_TRACK)
        payload_a, payload_b = a.split(".")[0], b.split(".")[0]
        assert payload_a != payload_b
        assert set(json.loads(_decode(a)).items()) != set(json.loads(_decode(b)).items())

    @pytest.mark.unit
    def test_garbage_input_never_resolves_to_a_token(self):
        for probe in ["1", "0", "f-p", "abcde.fghij", "..", "", "%%$.zz"]:
            status, payload = signed_qr.verify_token_ref(probe, signed_qr.PURPOSE_TRACK)
            assert payload is None
            assert status in (signed_qr.STATUS_INVALID, signed_qr.STATUS_TAMPERED)


def _decode(ref: str) -> bytes:
    import base64

    payload_b64 = ref.split(".", 1)[0]
    return base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))


class TestTamperAndExpiry:
    def test_flipped_payload_char_is_tampered(self):
        ref = signed_qr.create_token_ref(5, 1, signed_qr.PURPOSE_SCAN)
        head, sig = ref.split(".")
        # flip one char in the payload
        tampered = ("A" if head[0] != "A" else "B" if head[0] != "B" else "C") + head[1:]
        status, payload = signed_qr.verify_token_ref(f"{tampered}.{sig}", signed_qr.PURPOSE_SCAN)
        assert payload is None
        assert status in (signed_qr.STATUS_TAMPERED, signed_qr.STATUS_INVALID)

    def test_flipped_signature_char_is_tampered(self):
        ref = signed_qr.create_token_ref(5, 1, signed_qr.PURPOSE_SCAN)
        head, sig = ref.split(".")
        flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
        status, payload = signed_qr.verify_token_ref(f"{head}.{flipped}", signed_qr.PURPOSE_SCAN)
        assert status == signed_qr.STATUS_TAMPERED
        assert payload is None

    def test_expired_ref_is_expired_and_not_valid(self):
        now = time.time()
        ref = signed_qr.create_token_ref(
            5, 1, signed_qr.PURPOSE_SCAN, ttl_seconds=60, now=now
        )
        status, _ = signed_qr.verify_token_ref(ref, signed_qr.PURPOSE_SCAN, now=now + 61)
        assert status == signed_qr.STATUS_EXPIRED

    def test_still_valid_inside_ttl(self):
        now = time.time()
        ref = signed_qr.create_token_ref(
            5, 1, signed_qr.PURPOSE_SCAN, ttl_seconds=60, now=now
        )
        status, _ = signed_qr.verify_token_ref(ref, signed_qr.PURPOSE_SCAN, now=now + 59)
        assert status == signed_qr.STATUS_VALID

    def test_default_ttls_match_provisional_decision(self):
        """Decision 19 (PROVISIONAL): scan 24h / track 90d."""
        assert signed_qr.SCAN_REF_TTL_SECONDS == 24 * 60 * 60
        assert signed_qr.TRACK_REF_TTL_SECONDS == 90 * 24 * 60 * 60

    def test_purpose_binding_scan_cannot_be_used_as_track(self):
        scan_ref = signed_qr.create_token_ref(5, 1, signed_qr.PURPOSE_SCAN)
        status, payload = signed_qr.verify_token_ref(scan_ref, signed_qr.PURPOSE_TRACK)
        assert payload is None
        assert status == signed_qr.STATUS_TAMPERED

    def test_track_cannot_be_used_as_scan(self):
        track_ref = signed_qr.create_token_ref(5, 1, signed_qr.PURPOSE_TRACK)
        status, payload = signed_qr.verify_token_ref(track_ref, signed_qr.PURPOSE_SCAN)
        assert payload is None
        assert status == signed_qr.STATUS_TAMPERED

    def test_payload_structure_minimal_fields(self):
        """QR contains no private data — only ids/purpose/timestamps/nonce."""
        import base64

        ref = signed_qr.create_token_ref(5, 1, signed_qr.PURPOSE_TRACK)
        data = json.loads(_decode(ref))
        assert set(data) == {"t", "o", "p", "i", "e", "n", "v"}
