// Command gen reads the controller's OpenAPI document and writes the provider's resource table.
//
// Usage (from the provider directory):
//
//	go run ./internal/gen -spec ../docs/api/openapi.json -out internal/provider/zz_generated.go
//
// The output is data only: for every object kind the attribute tree of its create body, the keys
// its patch body accepts and any constant discriminator. Behaviour lives in the hand-written
// files of internal/provider. `-check` compares instead of writing, so CI can fail when the
// committed table is stale.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"go/format"
	"os"
	"sort"
	"strings"
)

// kind names mirror the constants in internal/provider/attr.go.
const (
	kString = "KindString"
	kInt    = "KindInt"
	kFloat  = "KindFloat"
	kBool   = "KindBool"
	kList   = "KindList"
	kMap    = "KindMap"
	kObject = "KindObject"
	kJSON   = "KindJSON"
)

type attr struct {
	name     string
	kind     string
	required bool
	doc      string
	elem     *attr
	attrs    []attr
	enum     []string
}

type definition struct {
	typeName    string
	slug        string
	doc         string
	resource    bool
	consts      map[string]string
	patchKeys   []string
	attrs       []attr
	discrimKey  string
	discrimWant string
}

type spec struct {
	paths   map[string]any
	schemas map[string]any
}

func main() {
	specPath := flag.String("spec", "../docs/api/openapi.json", "OpenAPI document")
	out := flag.String("out", "internal/provider/zz_generated.go", "generated Go file")
	check := flag.Bool("check", false, "fail if the output file differs instead of writing it")
	flag.Parse()

	raw, err := os.ReadFile(*specPath)
	must(err)
	var doc map[string]any
	must(json.Unmarshal(raw, &doc))
	s := spec{paths: obj(doc["paths"]), schemas: obj(obj(doc["components"])["schemas"])}

	code := render(s.definitions())
	formatted, err := format.Source(code)
	must(err)
	if *check {
		current, err := os.ReadFile(*out)
		if err != nil || !bytes.Equal(bytes.ReplaceAll(current, []byte("\r\n"), []byte("\n")), formatted) {
			fmt.Fprintf(os.Stderr, "%s is out of date; run go generate ./...\n", *out)
			os.Exit(1)
		}
		return
	}
	must(os.WriteFile(*out, formatted, 0o644))
}

func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func obj(v any) map[string]any {
	m, _ := v.(map[string]any)
	return m
}

func list(v any) []any {
	l, _ := v.([]any)
	return l
}

func str(v any) string {
	s, _ := v.(string)
	return s
}

func sortedKeys(m map[string]any) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// collections lists the tenant object kinds: slug, singular type name, and whether the
// collection holds several variants chosen by a discriminator.
var collections = []struct {
	slug     string
	typeName string
	noCreate bool // data source only
}{
	{"networks", "network", false},
	{"vip-pools", "vip_pool", false},
	{"se-groups", "se_group", false},
	{"virtual-services", "virtual_service", false},
	{"pools", "pool", false},
	{"health-monitors", "health_monitor", false},
	{"certificates", "certificate", true},
	{"policy-sets", "policy_set", false},
	{"routing-profiles", "routing_profile", false},
}

func (s spec) definitions() []definition {
	var defs []definition
	for _, c := range collections {
		path := obj(s.paths["/api/v1/tenants/{t}/"+c.slug])
		post := obj(path["post"])
		body := obj(obj(obj(obj(post["requestBody"])["content"])["application/json"])["schema"])
		patchPath := obj(s.paths["/api/v1/tenants/{t}/"+c.slug+"/{object_id}"])
		patchBody := obj(obj(obj(obj(obj(patchPath["patch"])["requestBody"])["content"])["application/json"])["schema"])

		if one := list(body["oneOf"]); len(one) > 0 {
			mapping := obj(obj(body["discriminator"])["mapping"])
			key := str(obj(body["discriminator"])["propertyName"])
			for _, variant := range sortedKeys(mapping) {
				create := s.resolve(map[string]any{"$ref": mapping[variant]})
				patch := s.patchFor(patchBody, variant, key)
				d := s.define(variant+"_"+c.typeName, c.slug, create, patch, !c.noCreate)
				d.discrimKey, d.discrimWant = key, variant
				defs = append(defs, d)
			}
			continue
		}
		defs = append(defs, s.define(c.typeName, c.slug, s.resolve(body), s.resolve(patchBody), !c.noCreate))
	}
	return defs
}

// patchFor picks the patch variant for one discriminator value (or the only patch schema).
func (s spec) patchFor(patchBody map[string]any, variant, key string) map[string]any {
	options := list(patchBody["oneOf"])
	if len(options) == 0 {
		return s.resolve(patchBody)
	}
	mapping := obj(obj(patchBody["discriminator"])["mapping"])
	if ref, ok := mapping[variant]; ok {
		return s.resolve(map[string]any{"$ref": ref})
	}
	for _, o := range options {
		r := s.resolve(obj(o))
		if str(obj(obj(r["properties"])[key])["const"]) == variant {
			return r
		}
	}
	return map[string]any{}
}

func (s spec) define(typeName, slug string, create, patch map[string]any, resource bool) definition {
	d := definition{typeName: typeName, slug: slug, resource: resource, consts: map[string]string{}}
	d.doc = firstParagraph(str(create["description"]))
	required := map[string]bool{}
	for _, r := range list(create["required"]) {
		required[str(r)] = true
	}
	props := obj(create["properties"])
	for _, name := range sortedKeys(props) {
		if c := str(obj(props[name])["const"]); c != "" {
			d.consts[name] = c
			continue
		}
		a := s.attribute(name, obj(props[name]))
		a.required = required[name]
		d.attrs = append(d.attrs, a)
	}
	for _, name := range sortedKeys(obj(patch["properties"])) {
		d.patchKeys = append(d.patchKeys, name)
	}
	return d
}

// resolve follows a $ref (and a lone allOf) to the schema it names, keeping sibling keywords.
func (s spec) resolve(sch map[string]any) map[string]any {
	for i := 0; i < 8; i++ {
		if ref := str(sch["$ref"]); ref != "" {
			target := obj(s.schemas[ref[strings.LastIndex(ref, "/")+1:]])
			merged := map[string]any{}
			for k, v := range target {
				merged[k] = v
			}
			for k, v := range sch {
				if k != "$ref" {
					merged[k] = v
				}
			}
			sch = merged
			continue
		}
		if all := list(sch["allOf"]); len(all) == 1 {
			sch = s.resolve(obj(all[0]))
			continue
		}
		break
	}
	return sch
}

func (s spec) attribute(name string, sch map[string]any) attr {
	sch = s.resolve(sch)
	a := attr{name: name, doc: firstParagraph(str(sch["description"]))}
	// X | null is just X; several non-null alternatives are one string when all are strings
	// (an IPv4 or IPv6 address), and otherwise opaque JSON.
	if alts := list(sch["anyOf"]); len(alts) > 0 {
		var kept []map[string]any
		for _, alt := range alts {
			r := s.resolve(obj(alt))
			if str(r["type"]) != "null" {
				kept = append(kept, r)
			}
		}
		switch {
		case len(kept) == 1:
			inner := s.attribute(name, kept[0])
			if inner.doc == "" {
				inner.doc = a.doc
			}
			return inner
		case allStrings(kept):
			a.kind = kString
			return a
		default:
			a.kind = kJSON
			return a
		}
	}
	if len(list(sch["oneOf"])) > 0 {
		a.kind = kJSON
		return a
	}
	if enum := list(sch["enum"]); len(enum) > 0 {
		a.kind = kString
		for _, e := range enum {
			a.enum = append(a.enum, fmt.Sprint(e))
		}
		return a
	}
	switch str(sch["type"]) {
	case "string":
		a.kind = kString
	case "integer":
		a.kind = kInt
	case "number":
		a.kind = kFloat
	case "boolean":
		a.kind = kBool
	case "array":
		a.kind = kList
		e := s.attribute("", obj(sch["items"]))
		a.elem = &e
	case "object":
		props := obj(sch["properties"])
		if len(props) == 0 {
			if extra := obj(sch["additionalProperties"]); len(extra) > 0 {
				e := s.attribute("", extra)
				if e.kind != kJSON {
					a.kind, a.elem = kMap, &e
					return a
				}
			}
			a.kind = kJSON
			return a
		}
		a.kind = kObject
		required := map[string]bool{}
		for _, r := range list(sch["required"]) {
			required[str(r)] = true
		}
		for _, n := range sortedKeys(props) {
			child := s.attribute(n, obj(props[n]))
			child.required = required[n]
			a.attrs = append(a.attrs, child)
		}
	default:
		a.kind = kJSON
	}
	return a
}

func allStrings(alts []map[string]any) bool {
	for _, a := range alts {
		if str(a["type"]) != "string" {
			return false
		}
	}
	return len(alts) > 0
}

func firstParagraph(text string) string {
	text = strings.TrimSpace(text)
	if i := strings.Index(text, "\n\n"); i >= 0 {
		text = text[:i]
	}
	return strings.Join(strings.Fields(text), " ")
}

// -- output ----------------------------------------------------------------------------------

func render(defs []definition) []byte {
	var b bytes.Buffer
	b.WriteString("// Code generated by internal/gen from docs/api/openapi.json. DO NOT EDIT.\n\n")
	b.WriteString("package provider\n\n")
	b.WriteString("// Definitions describes every object kind the controller API exposes.\n")
	b.WriteString("var Definitions = []Definition{\n")
	for _, d := range defs {
		fmt.Fprintf(&b, "\t{\n\t\tTypeName: %q,\n\t\tSlug: %q,\n\t\tDoc: %q,\n\t\tResource: %v,\n", d.typeName, d.slug, d.doc, d.resource)
		if d.discrimKey != "" {
			fmt.Fprintf(&b, "\t\tDiscriminator: %q,\n\t\tVariant: %q,\n", d.discrimKey, d.discrimWant)
		}
		if len(d.consts) > 0 {
			b.WriteString("\t\tConsts: map[string]string{")
			for _, k := range sortedStringKeys(d.consts) {
				fmt.Fprintf(&b, "%q: %q, ", k, d.consts[k])
			}
			b.WriteString("},\n")
		}
		fmt.Fprintf(&b, "\t\tPatchKeys: %#v,\n", d.patchKeys)
		b.WriteString("\t\tAttrs: []Attr{\n")
		for _, a := range d.attrs {
			writeAttr(&b, a, 3)
		}
		b.WriteString("\t\t},\n\t},\n")
	}
	b.WriteString("}\n")
	return b.Bytes()
}

func sortedStringKeys(m map[string]string) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func writeAttr(b *bytes.Buffer, a attr, depth int) {
	pad := strings.Repeat("\t", depth)
	fmt.Fprintf(b, "%s{Name: %q, Kind: %s", pad, a.name, a.kind)
	if a.required {
		b.WriteString(", Required: true")
	}
	if a.doc != "" {
		fmt.Fprintf(b, ", Doc: %q", a.doc)
	}
	if len(a.enum) > 0 {
		fmt.Fprintf(b, ", Enum: %#v", a.enum)
	}
	if a.elem != nil {
		b.WriteString(", Elem: &")
		writeInline(b, *a.elem)
	}
	if len(a.attrs) > 0 {
		b.WriteString(", Attrs: []Attr{\n")
		for _, c := range a.attrs {
			writeAttr(b, c, depth+1)
		}
		fmt.Fprintf(b, "%s}", pad)
	}
	b.WriteString("},\n")
}

func writeInline(b *bytes.Buffer, a attr) {
	fmt.Fprintf(b, "Attr{Kind: %s", a.kind)
	if len(a.enum) > 0 {
		fmt.Fprintf(b, ", Enum: %#v", a.enum)
	}
	if a.elem != nil {
		b.WriteString(", Elem: &")
		writeInline(b, *a.elem)
	}
	if len(a.attrs) > 0 {
		b.WriteString(", Attrs: []Attr{\n")
		for _, c := range a.attrs {
			writeAttr(b, c, 5)
		}
		b.WriteString("}")
	}
	b.WriteString("}")
}
