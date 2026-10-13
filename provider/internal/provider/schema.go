// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

package provider

import (
	"fmt"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	dschema "github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

func describe(a Attr) string {
	text := a.Doc
	if len(a.Enum) > 0 {
		text = strings.TrimSpace(text + " One of: " + strings.Join(a.Enum, ", ") + ".")
	}
	if a.Kind == KindJSON {
		text = strings.TrimSpace(text + " A JSON document; build it with jsonencode().")
	}
	return text
}

// elemType is the framework type of a list or map element.
func elemType(a Attr) attr.Type {
	switch a.Kind {
	case KindString, KindJSON:
		return types.StringType
	case KindInt:
		return types.Int64Type
	case KindFloat:
		return types.Float64Type
	case KindBool:
		return types.BoolType
	case KindList:
		return types.ListType{ElemType: elemType(*a.Elem)}
	case KindMap:
		return types.MapType{ElemType: elemType(*a.Elem)}
	case KindObject:
		fields := map[string]attr.Type{}
		for _, c := range a.Attrs {
			fields[c.Name] = elemType(c)
		}
		return types.ObjectType{AttrTypes: fields}
	}
	return types.StringType
}

// resourceAttributes builds the resource attributes of an object. Fields the caller may leave out
// are optional and computed: the controller fills defaults, and Terraform needs to know the
// result is not an inconsistency.
func resourceAttributes(attrs []Attr) map[string]schema.Attribute {
	out := map[string]schema.Attribute{}
	for _, a := range attrs {
		out[a.Name] = resourceAttribute(a)
	}
	return out
}

func resourceAttribute(a Attr) schema.Attribute {
	required := a.Required
	optional := !required
	computed := !required
	desc := describe(a)
	switch a.Kind {
	case KindInt:
		return schema.Int64Attribute{Required: required, Optional: optional, Computed: computed, Description: desc}
	case KindFloat:
		return schema.Float64Attribute{Required: required, Optional: optional, Computed: computed, Description: desc}
	case KindBool:
		return schema.BoolAttribute{Required: required, Optional: optional, Computed: computed, Description: desc}
	case KindList:
		if a.Elem.Kind == KindObject {
			return schema.ListNestedAttribute{
				Required: required, Optional: optional, Computed: computed, Description: desc,
				NestedObject: schema.NestedAttributeObject{Attributes: resourceAttributes(a.Elem.Attrs)},
			}
		}
		return schema.ListAttribute{Required: required, Optional: optional, Computed: computed, Description: desc, ElementType: elemType(*a.Elem)}
	case KindMap:
		return schema.MapAttribute{Required: required, Optional: optional, Computed: computed, Description: desc, ElementType: elemType(*a.Elem)}
	case KindObject:
		return schema.SingleNestedAttribute{
			Required: required, Optional: optional, Computed: computed, Description: desc,
			Attributes: resourceAttributes(a.Attrs),
		}
	default: // strings and opaque JSON
		return schema.StringAttribute{Required: required, Optional: optional, Computed: computed, Description: desc}
	}
}

// ResourceSchema is the schema of the Terraform resource for d.
func ResourceSchema(d Definition) schema.Schema {
	attrs := resourceAttributes(d.Attrs)
	attrs["id"] = schema.StringAttribute{
		Computed:      true,
		Description:   "Object id (UUID). Chosen when the object is created; use it to reference the object.",
		PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
	}
	attrs["version"] = schema.Int64Attribute{
		Computed:    true,
		Description: "Object version, changed by every applied update. Used as the ETag.",
	}
	return schema.Schema{Description: docOr(d), Attributes: attrs}
}

func docOr(d Definition) string {
	if d.Doc != "" {
		return d.Doc
	}
	return fmt.Sprintf("Manages one %s of the tenant.", strings.ReplaceAll(d.TypeName, "_", " "))
}

func dataAttributes(attrs []Attr) map[string]dschema.Attribute {
	out := map[string]dschema.Attribute{}
	for _, a := range attrs {
		out[a.Name] = dataAttribute(a)
	}
	return out
}

func dataAttribute(a Attr) dschema.Attribute {
	desc := describe(a)
	switch a.Kind {
	case KindInt:
		return dschema.Int64Attribute{Computed: true, Description: desc}
	case KindFloat:
		return dschema.Float64Attribute{Computed: true, Description: desc}
	case KindBool:
		return dschema.BoolAttribute{Computed: true, Description: desc}
	case KindList:
		if a.Elem.Kind == KindObject {
			return dschema.ListNestedAttribute{
				Computed: true, Description: desc,
				NestedObject: dschema.NestedAttributeObject{Attributes: dataAttributes(a.Elem.Attrs)},
			}
		}
		return dschema.ListAttribute{Computed: true, Description: desc, ElementType: elemType(*a.Elem)}
	case KindMap:
		return dschema.MapAttribute{Computed: true, Description: desc, ElementType: elemType(*a.Elem)}
	case KindObject:
		return dschema.SingleNestedAttribute{Computed: true, Description: desc, Attributes: dataAttributes(a.Attrs)}
	default:
		return dschema.StringAttribute{Computed: true, Description: desc}
	}
}

// DataSourceSchema is the schema of the data source for d: look an object up by name.
func DataSourceSchema(d Definition) dschema.Schema {
	attrs := dataAttributes(d.Attrs)
	attrs["name"] = dschema.StringAttribute{Required: true, Description: "Name of the object to look up."}
	attrs["id"] = dschema.StringAttribute{Computed: true, Description: "Object id (UUID)."}
	attrs["version"] = dschema.Int64Attribute{Computed: true, Description: "Object version."}
	return dschema.Schema{Description: fmt.Sprintf("Looks up one %s by name. %s", strings.ReplaceAll(d.TypeName, "_", " "), docOr(d)), Attributes: attrs}
}
