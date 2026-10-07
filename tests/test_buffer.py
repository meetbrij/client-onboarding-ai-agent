from onboarding.tools.buffer import DocumentBuffer


def test_take_removes_the_bytes():
    b = DocumentBuffer()
    b.put("c", "d1", b"abc")
    assert len(b) == 1 and b.take("c", "d1") == b"abc" and b.take("c", "d1") is None and len(b) == 0


def test_expired_items_are_gone():
    now = [0.0]
    b = DocumentBuffer(ttl_s=10, clock=lambda: now[0])
    b.put("c", "d1", b"abc")
    now[0] = 9.9
    assert len(b) == 1
    now[0] = 10.0
    assert b.take("c", "d1") is None and len(b) == 0


def test_keys_are_per_case_and_document():
    b = DocumentBuffer()
    b.put("c1", "d", b"1")
    b.put("c2", "d", b"2")
    assert b.take("c2", "d") == b"2" and b.take("c1", "d") == b"1" and b.take("c1", "other") is None
