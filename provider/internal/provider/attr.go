// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

package provider

//go:generate go run ../gen -spec ../../../docs/api/openapi.json -out zz_generated.go

// Kind is the shape of one attribute.
type Kind int

const (
	// KindString is a string, including enums, UUIDs and IP addresses.
	KindString Kind = iota
	// KindInt is a whole number.
	KindInt
	// KindFloat is any number.
	KindFloat
	// KindBool is a boolean.
	KindBool
	// KindList is an ordered list of Elem.
	KindList
	// KindMap is a map from string to Elem.
	KindMap
	// KindObject is a nested object with Attrs.
	KindObject
	// KindJSON is a value the generator could not type; it is a JSON document held in a string.
	KindJSON
)

// Attr is one attribute of a generated definition.
type Attr struct {
	Name     string
	Kind     Kind
	Required bool
	Doc      string
	Elem     *Attr
	Attrs    []Attr
	Enum     []string
}

// Definition describes one object kind of the controller API.
type Definition struct {
	// TypeName is the suffix of the Terraform type: switchyard_<TypeName>.
	TypeName string
	// Slug is the collection in the API path: /tenants/{t}/<Slug>.
	Slug string
	// Doc is the description shown in the schema.
	Doc string
	// Resource is false for kinds that only exist as data sources.
	Resource bool
	// Discriminator and Variant select one kind among several that share a collection.
	Discriminator string
	Variant       string
	// Consts are body fields with one fixed value (the discriminator), sent on create.
	Consts map[string]string
	// PatchKeys are the fields an update may send.
	PatchKeys []string
	// Attrs are the fields of the create body.
	Attrs []Attr
}
