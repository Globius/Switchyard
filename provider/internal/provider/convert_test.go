package provider

import (
	"encoding/json"
	"math/big"
	"strings"
	"testing"

	"github.com/hashicorp/terraform-plugin-go/tftypes"
)

func TestSameTextAcceptsTheSameAddressWrittenDifferently(t *testing.T) {
	for _, c := range []struct {
		a, b string
		want bool
	}{
		{"2001:db8:0:0:0:0:0:1", "2001:db8::1", true},
		{"2001:DB8::1", "2001:db8::1", true},
		{"10.0.0.0/24", "10.0.0.0/24", true},
		{"2001:db8:0::/64", "2001:db8::/64", true},
		{"10.0.0.1", "10.0.0.2", false},
		{"web", "api", false},
	} {
		if got := sameText(c.a, c.b); got != c.want {
			t.Errorf("sameText(%q, %q) = %v", c.a, c.b, got)
		}
	}
}

func TestAJSONAttributeKeepsTheUsersSpellingWhenTheMeaningIsTheSame(t *testing.T) {
	typ := tftypes.Object{AttributeTypes: map[string]tftypes.Type{"doc": tftypes.String}}
	paths := jsonPaths{"doc": true}
	prior := tftypes.NewValue(typ, map[string]tftypes.Value{"doc": tftypes.NewValue(tftypes.String, `{ "b": 1,  "a": [1, 2] }`)})
	api := map[string]any{"doc": map[string]any{"a": []any{json.Number("1"), json.Number("2")}, "b": json.Number("1")}}
	v, err := fromJSON(typ, api, prior, paths, "")
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]tftypes.Value
	_ = v.As(&fields)
	if got := str(t, fields["doc"]); got != `{ "b": 1,  "a": [1, 2] }` {
		t.Errorf("got %s; a re-ordered but equal document must not show as a change", got)
	}
	// a real change comes through
	api["doc"] = map[string]any{"a": []any{}, "b": json.Number("2")}
	v, _ = fromJSON(typ, api, prior, paths, "")
	_ = v.As(&fields)
	if got := str(t, fields["doc"]); !strings.Contains(got, `"b":2`) {
		t.Errorf("got %s", got)
	}
}

func TestToJSONLeavesOutUnknownAndNullAndParsesJSONAttributes(t *testing.T) {
	typ := tftypes.Object{AttributeTypes: map[string]tftypes.Type{
		"name":   tftypes.String,
		"port":   tftypes.Number,
		"weight": tftypes.Number,
		"doc":    tftypes.String,
		"later":  tftypes.String,
		"empty":  tftypes.String,
		"tags":   tftypes.List{ElementType: tftypes.String},
	}}
	v := tftypes.NewValue(typ, map[string]tftypes.Value{
		"name":   tftypes.NewValue(tftypes.String, "web"),
		"port":   tftypes.NewValue(tftypes.Number, big.NewFloat(8080)),
		"weight": tftypes.NewValue(tftypes.Number, big.NewFloat(0.5)),
		"doc":    tftypes.NewValue(tftypes.String, `{"a":1}`),
		"later":  tftypes.NewValue(tftypes.String, tftypes.UnknownValue),
		"empty":  tftypes.NewValue(tftypes.String, nil),
		"tags":   tftypes.NewValue(tftypes.List{ElementType: tftypes.String}, []tftypes.Value{tftypes.NewValue(tftypes.String, "x")}),
	})
	got, ok, err := toJSON(v, jsonPaths{"doc": true}, "")
	if err != nil || !ok {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(got)
	want := `{"doc":{"a":1},"name":"web","port":8080,"tags":["x"],"weight":0.5}`
	if string(raw) != want {
		t.Errorf("got %s, want %s", raw, want)
	}
	if _, _, err := toJSON(tftypes.NewValue(tftypes.String, "{nope"), jsonPaths{"": true}, ""); err == nil {
		t.Error("a malformed JSON attribute must be refused before it is sent")
	}
}

func TestEveryJSONFallbackIsFoundByPath(t *testing.T) {
	attrs := []Attr{
		{Name: "a", Kind: KindJSON},
		{Name: "o", Kind: KindObject, Attrs: []Attr{{Name: "b", Kind: KindJSON}}},
		{Name: "l", Kind: KindList, Elem: &Attr{Kind: KindObject, Attrs: []Attr{{Name: "c", Kind: KindJSON}}}},
	}
	paths := newJSONPaths(attrs)
	for _, p := range []string{"a", "o.b", "l.*.c"} {
		if !paths[p] {
			t.Errorf("missing %s in %v", p, paths)
		}
	}
}
