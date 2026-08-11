"""RFC 8785 (JCS) conformance tests for byoai.recorder.canonical."""

from __future__ import annotations

import hashlib
import json
import math

import pytest

from byoai.recorder.canonical import (
    CanonicalizationError,
    canonical_dumps,
    canonicalize,
    serialize_number,
    sha256_hex,
)


class TestNumbers:
    """ECMAScript Number::toString cases, incl. the RFC 8785 appendix ones."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.0, "0"),
            (-0.0, "0"),
            (1.0, "1"),
            (-1.0, "-1"),
            (1.5, "1.5"),
            (100.0, "100"),
            (1e21, "1e+21"),
            (1e22, "1e+22"),
            (1e-7, "1e-7"),
            (1e-6, "0.000001"),
            (0.000001, "0.000001"),
            (1e20, "100000000000000000000"),
            (1.1e21, "1.1e+21"),
            (333333333.33333329, "333333333.3333333"),
            (5e-324, "5e-324"),
            (1.7976931348623157e308, "1.7976931348623157e+308"),
            (-1e-7, "-1e-7"),
            (2.2250738585072014e-308, "2.2250738585072014e-308"),
        ],
    )
    def test_es_number_to_string(self, value: float, expected: str) -> None:
        assert serialize_number(value) == expected

    def test_integers_are_exact(self) -> None:
        assert serialize_number(0) == "0"
        assert serialize_number(-17) == "-17"
        # Beyond 2**53: kept exact rather than rounded through a double.
        assert serialize_number(9007199254740993) == "9007199254740993"

    def test_float_valued_integers_lose_the_point(self) -> None:
        assert canonicalize({"a": 1.0, "b": 2.0e3}) == b'{"a":1,"b":2000}'

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_nan_and_infinity_rejected(self, bad: float) -> None:
        with pytest.raises(CanonicalizationError):
            canonicalize({"x": bad})

    def test_round_trips_through_json(self) -> None:
        for value in (1e21, 1e-7, 333333333.33333329, 5e-324, 1.5):
            assert json.loads(serialize_number(value)) == value

    def test_no_nan_leak_via_math(self) -> None:
        assert math.isnan(float("nan"))  # sanity for the parametrization above


class TestStrings:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("", '""'),
            ("abc", '"abc"'),
            ('"', '"\\""'),
            ("\\", '"\\\\"'),
            ("\b\t\n\f\r", '"\\b\\t\\n\\f\\r"'),
            ("\x00", '"\\u0000"'),
            ("\x1f", '"\\u001f"'),
            # Not escaped: DEL, solidus, and anything above U+001F.
            ("\x7f", '"\x7f"'),
            ("/", '"/"'),
            ("é", '"é"'),
            ("\U0001f600", '"\U0001f600"'),
        ],
    )
    def test_escaping(self, value: str, expected: str) -> None:
        assert canonical_dumps(value) == expected

    def test_non_ascii_stays_literal_utf8(self) -> None:
        assert canonicalize("€") == b'"\xe2\x82\xac"'

    def test_lone_surrogate_rejected(self) -> None:
        with pytest.raises(CanonicalizationError):
            canonicalize("\ud800")
        with pytest.raises(CanonicalizationError):
            canonicalize({"\udfff": 1})


class TestKeyOrdering:
    def test_plain_ascii_sort(self) -> None:
        assert canonicalize({"b": 1, "a": 2, "C": 3}) == b'{"C":3,"a":2,"b":1}'

    def test_insertion_order_irrelevant(self) -> None:
        first = {"z": 1, "a": {"y": 2, "b": 3}}
        second = {"a": {"b": 3, "y": 2}, "z": 1}
        assert canonicalize(first) == canonicalize(second)

    def test_astral_plane_sorts_by_utf16_code_unit(self) -> None:
        """The JCS test-vector case: code point order != UTF-16 order.

        U+1F600 encodes as the surrogate pair D83D DE00, so under UTF-16 code
        unit order it sorts *before* U+E000/U+FB33, even though its code point
        is far higher. Naive Python sorting would put it last.
        """
        obj = {"€": 1, "\U0001f600": 2, "דּ": 3, "\U0001d11e": 4, "a": 5}
        expected_order = [
            "a",
            "€",
            "\U0001d11e",
            "\U0001f600",
            "דּ",
        ]
        out = canonical_dumps(obj)
        positions = [out.index(f'"{k}"') for k in expected_order]
        assert positions == sorted(positions)
        # Python's own code-point sort disagrees, which is the whole point.
        assert sorted(obj) != expected_order

    def test_empty_key_sorts_first(self) -> None:
        assert canonicalize({"a": 1, "": 2}) == b'{"":2,"a":1}'

    def test_prefix_key_sorts_first(self) -> None:
        assert canonicalize({"ab": 1, "a": 2}) == b'{"a":2,"ab":1}'

    def test_non_string_key_rejected(self) -> None:
        with pytest.raises(CanonicalizationError):
            canonicalize({1: "a"})


class TestStructure:
    def test_scalars(self) -> None:
        assert canonicalize(None) == b"null"
        assert canonicalize(True) == b"true"
        assert canonicalize(False) == b"false"

    def test_no_insignificant_whitespace(self) -> None:
        obj = {"a": [1, 2, {"b": None}], "c": True}
        assert canonicalize(obj) == b'{"a":[1,2,{"b":null}],"c":true}'

    def test_arrays_keep_order(self) -> None:
        assert canonicalize(["b", "a", 3]) == b'["b","a",3]'

    def test_tuples_serialize_as_arrays(self) -> None:
        assert canonicalize(("a", 1)) == b'["a",1]'

    def test_empty_containers(self) -> None:
        assert canonicalize({}) == b"{}"
        assert canonicalize([]) == b"[]"

    def test_bools_are_not_numbers(self) -> None:
        assert canonicalize({"t": True, "f": False}) == b'{"f":false,"t":true}'

    def test_unsupported_type_rejected(self) -> None:
        with pytest.raises(CanonicalizationError):
            canonicalize({"a": object()})
        with pytest.raises(CanonicalizationError):
            canonicalize({"a": {1, 2}})

    def test_rfc8785_appendix_style_vector(self) -> None:
        """The RFC's worked example: mixed literals, ordering and numbers."""
        obj = {
            "numbers": [333333333.33333329, 1e30, 4.5, 2e-3, 1e-27],
            "string": "\u20ac$\x0f\nA'B\"\\\"/",
            "literals": [None, True, False],
        }
        expected = (
            '{"literals":[null,true,false],'
            '"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
            '"string":"\u20ac$\\u000f\\nA\'B\\"\\\\\\"/"}'
        )
        assert canonical_dumps(obj) == expected

    def test_output_is_utf8_bytes(self) -> None:
        out = canonicalize({"k": "\U0001f600"})
        assert isinstance(out, bytes)
        assert out.decode("utf-8") == '{"k":"\U0001f600"}'

    def test_deep_nesting_is_deterministic(self) -> None:
        deep: object = {"leaf": 1.0}
        for i in range(50):
            deep = {f"k{i}": deep, "other": i}
        assert canonicalize(deep) == canonicalize(json.loads(canonical_dumps(deep)))


class TestSha256Hex:
    def test_prefixed_hex(self) -> None:
        digest = sha256_hex(b"abc")
        assert digest.startswith("sha256:")
        assert digest == "sha256:" + hashlib.sha256(b"abc").hexdigest()
        assert len(digest) == len("sha256:") + 64

    def test_stable_over_canonical_form(self) -> None:
        a = sha256_hex(canonicalize({"x": 1, "y": [1.0, "z"]}))
        b = sha256_hex(canonicalize({"y": [1, "z"], "x": 1}))
        assert a == b
