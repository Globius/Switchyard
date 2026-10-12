// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

package provider

import (
	"encoding/json"
	"fmt"
	"math/big"
	"net/netip"
	"reflect"

	"github.com/hashicorp/terraform-plugin-go/tftypes"
)

// jsonPaths holds the attribute paths ("members.cidrs") whose value is a JSON document in a string.
type jsonPaths map[string]bool

func collectJSON(attrs []Attr, prefix string, out jsonPaths) {
	for _, a := range attrs {
		path := prefix + a.Name
		switch a.Kind {
		case KindJSON:
			out[path] = true
		case KindObject:
			collectJSON(a.Attrs, path+".", out)
		case KindList, KindMap:
			collectElemJSON(a.Elem, path, out)
		}
	}
}

func collectElemJSON(e *Attr, path string, out jsonPaths) {
	switch e.Kind {
	case KindJSON:
		out[path+".*"] = true
	case KindObject:
		collectJSON(e.Attrs, path+".*.", out)
	case KindList, KindMap:
		collectElemJSON(e.Elem, path+".*", out)
	}
}

func newJSONPaths(attrs []Attr) jsonPaths {
	out := jsonPaths{}
	collectJSON(attrs, "", out)
	return out
}

func join(path, name string) string {
	if path == "" {
		return name
	}
	return path + "." + name
}

// toJSON turns a planned Terraform value into the request body fragment. Unknown and null values
// are left out so the controller applies its own defaults; the second result says whether the
// value is present at all.
func toJSON(v tftypes.Value, paths jsonPaths, path string) (any, bool, error) {
	if !v.IsKnown() || v.IsNull() {
		return nil, false, nil
	}
	t := v.Type()
	switch {
	case t.Is(tftypes.Object{}):
		var fields map[string]tftypes.Value
		if err := v.As(&fields); err != nil {
			return nil, false, err
		}
		out := map[string]any{}
		for name, child := range fields {
			j, ok, err := toJSON(child, paths, join(path, name))
			if err != nil {
				return nil, false, err
			}
			if ok {
				out[name] = j
			}
		}
		return out, true, nil
	case t.Is(tftypes.List{}), t.Is(tftypes.Set{}), t.Is(tftypes.Tuple{}):
		var items []tftypes.Value
		if err := v.As(&items); err != nil {
			return nil, false, err
		}
		out := make([]any, 0, len(items))
		for _, item := range items {
			j, ok, err := toJSON(item, paths, path+".*")
			if err != nil {
				return nil, false, err
			}
			if !ok {
				j = nil
			}
			out = append(out, j)
		}
		return out, true, nil
	case t.Is(tftypes.Map{}):
		var items map[string]tftypes.Value
		if err := v.As(&items); err != nil {
			return nil, false, err
		}
		out := map[string]any{}
		for k, item := range items {
			if j, ok, err := toJSON(item, paths, path+".*"); err != nil {
				return nil, false, err
			} else if ok {
				out[k] = j
			}
		}
		return out, true, nil
	case t.Is(tftypes.String):
		var s string
		if err := v.As(&s); err != nil {
			return nil, false, err
		}
		if paths[path] {
			var doc any
			if err := json.Unmarshal([]byte(s), &doc); err != nil {
				return nil, false, fmt.Errorf("%s is not valid JSON: %w", path, err)
			}
			return doc, true, nil
		}
		return s, true, nil
	case t.Is(tftypes.Number):
		var f big.Float
		if err := v.As(&f); err != nil {
			return nil, false, err
		}
		if f.IsInt() {
			i, _ := f.Int64()
			return i, true, nil
		}
		x, _ := f.Float64()
		return x, true, nil
	case t.Is(tftypes.Bool):
		var b bool
		if err := v.As(&b); err != nil {
			return nil, false, err
		}
		return b, true, nil
	}
	return nil, false, fmt.Errorf("unsupported value type %s at %s", t, path)
}

// fromJSON builds the Terraform value of type t from the controller's JSON. prior is the value
// Terraform already holds (plan or state); where the controller's answer means the same as the
// prior value (a re-ordered JSON document, an IPv6 address written differently) the prior value
// is kept, because Terraform refuses a result that differs from the plan.
func fromJSON(t tftypes.Type, j any, prior tftypes.Value, paths jsonPaths, path string) (tftypes.Value, error) {
	if j == nil {
		return tftypes.NewValue(t, nil), nil
	}
	switch {
	case t.Is(tftypes.Object{}):
		obj, ok := j.(map[string]any)
		if !ok {
			return tftypes.Value{}, fmt.Errorf("%s: expected an object, got %T", path, j)
		}
		var priors map[string]tftypes.Value
		if prior.IsKnown() && !prior.IsNull() {
			_ = prior.As(&priors)
		}
		values := map[string]tftypes.Value{}
		for name, childType := range t.(tftypes.Object).AttributeTypes {
			child, err := fromJSON(childType, obj[name], priorOrNull(priors, name, childType), paths, join(path, name))
			if err != nil {
				return tftypes.Value{}, err
			}
			values[name] = child
		}
		return tftypes.NewValue(t, values), nil
	case t.Is(tftypes.List{}), t.Is(tftypes.Set{}):
		items, ok := j.([]any)
		if !ok {
			return tftypes.Value{}, fmt.Errorf("%s: expected a list, got %T", path, j)
		}
		var priors []tftypes.Value
		if prior.IsKnown() && !prior.IsNull() {
			_ = prior.As(&priors)
		}
		elem := elementType(t)
		values := make([]tftypes.Value, 0, len(items))
		for i, item := range items {
			p := tftypes.NewValue(elem, nil)
			if i < len(priors) {
				p = priors[i]
			}
			v, err := fromJSON(elem, item, p, paths, path+".*")
			if err != nil {
				return tftypes.Value{}, err
			}
			values = append(values, v)
		}
		return tftypes.NewValue(t, values), nil
	case t.Is(tftypes.Map{}):
		items, ok := j.(map[string]any)
		if !ok {
			return tftypes.Value{}, fmt.Errorf("%s: expected a map, got %T", path, j)
		}
		var priors map[string]tftypes.Value
		if prior.IsKnown() && !prior.IsNull() {
			_ = prior.As(&priors)
		}
		elem := t.(tftypes.Map).ElementType
		values := map[string]tftypes.Value{}
		for k, item := range items {
			v, err := fromJSON(elem, item, priorOrNull(priors, k, elem), paths, path+".*")
			if err != nil {
				return tftypes.Value{}, err
			}
			values[k] = v
		}
		return tftypes.NewValue(t, values), nil
	case t.Is(tftypes.String):
		return stringValue(t, j, prior, paths[path], path)
	case t.Is(tftypes.Number):
		switch n := j.(type) {
		case json.Number:
			f, _, err := big.ParseFloat(n.String(), 10, 512, big.ToNearestEven)
			if err != nil {
				return tftypes.Value{}, fmt.Errorf("%s: %w", path, err)
			}
			return tftypes.NewValue(t, f), nil
		case float64:
			return tftypes.NewValue(t, big.NewFloat(n)), nil
		}
		return tftypes.Value{}, fmt.Errorf("%s: expected a number, got %T", path, j)
	case t.Is(tftypes.Bool):
		b, ok := j.(bool)
		if !ok {
			return tftypes.Value{}, fmt.Errorf("%s: expected a boolean, got %T", path, j)
		}
		return tftypes.NewValue(t, b), nil
	}
	return tftypes.Value{}, fmt.Errorf("unsupported type %s at %s", t, path)
}

func elementType(t tftypes.Type) tftypes.Type {
	if l, ok := t.(tftypes.List); ok {
		return l.ElementType
	}
	return t.(tftypes.Set).ElementType
}

func priorOrNull(priors map[string]tftypes.Value, name string, t tftypes.Type) tftypes.Value {
	if p, ok := priors[name]; ok {
		return p
	}
	return tftypes.NewValue(t, nil)
}

func stringValue(t tftypes.Type, j any, prior tftypes.Value, isJSON bool, path string) (tftypes.Value, error) {
	var prev *string
	if prior.IsKnown() && !prior.IsNull() {
		var s string
		if prior.As(&s) == nil {
			prev = &s
		}
	}
	if isJSON {
		raw, err := json.Marshal(j)
		if err != nil {
			return tftypes.Value{}, fmt.Errorf("%s: %w", path, err)
		}
		if prev != nil && sameJSON(*prev, j) {
			return tftypes.NewValue(t, *prev), nil
		}
		return tftypes.NewValue(t, string(raw)), nil
	}
	s, ok := j.(string)
	if !ok {
		return tftypes.Value{}, fmt.Errorf("%s: expected a string, got %T", path, j)
	}
	if prev != nil && sameText(*prev, s) {
		return tftypes.NewValue(t, *prev), nil
	}
	return tftypes.NewValue(t, s), nil
}

// sameJSON reports whether the document in text means what j means.
func sameJSON(text string, j any) bool {
	var doc any
	if json.Unmarshal([]byte(text), &doc) != nil {
		return false
	}
	norm := func(v any) any {
		raw, _ := json.Marshal(v)
		var out any
		_ = json.Unmarshal(raw, &out)
		return out
	}
	return reflect.DeepEqual(norm(doc), norm(j))
}

// sameText reports whether a and b are equal, or the same IP address or network written
// differently (the controller prints addresses in canonical form).
func sameText(a, b string) bool {
	if a == b {
		return true
	}
	if x, err := netip.ParseAddr(a); err == nil {
		y, err := netip.ParseAddr(b)
		return err == nil && x == y
	}
	if x, err := netip.ParsePrefix(a); err == nil {
		y, err := netip.ParsePrefix(b)
		return err == nil && x == y
	}
	return false
}
