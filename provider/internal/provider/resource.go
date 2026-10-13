// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

package provider

import (
	"context"
	"fmt"

	"github.com/google/uuid"
	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
)

// genericResource implements one Terraform resource from a generated Definition.
type genericResource struct {
	def    Definition
	paths  jsonPaths
	client *Client
}

func newResource(d Definition) func() resource.Resource {
	return func() resource.Resource {
		return &genericResource{def: d, paths: newJSONPaths(d.Attrs)}
	}
}

var (
	_ resource.Resource                = (*genericResource)(nil)
	_ resource.ResourceWithConfigure   = (*genericResource)(nil)
	_ resource.ResourceWithImportState = (*genericResource)(nil)
)

func (r *genericResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_" + r.def.TypeName
}

func (r *genericResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = ResourceSchema(r.def)
}

func (r *genericResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	if req.ProviderData == nil {
		return
	}
	client, ok := req.ProviderData.(*Client)
	if !ok {
		resp.Diagnostics.AddError("Unexpected provider data", fmt.Sprintf("got %T", req.ProviderData))
		return
	}
	r.client = client
}

func (r *genericResource) stateType(ctx context.Context) (tftypes.Type, error) {
	return ResourceSchema(r.def).Type().TerraformType(ctx), nil
}

// body is the request body for a create or update: the planned values, minus the ones the
// controller owns.
func (r *genericResource) body(plan tftypes.Value, patch bool) (map[string]any, error) {
	raw, _, err := toJSON(plan, r.paths, "")
	if err != nil {
		return nil, err
	}
	all, _ := raw.(map[string]any)
	delete(all, "id")
	delete(all, "version")
	if !patch {
		for k, v := range r.def.Consts {
			all[k] = v
		}
		return all, nil
	}
	allowed := map[string]bool{}
	for _, k := range r.def.PatchKeys {
		allowed[k] = true
	}
	out := map[string]any{}
	for k, v := range all {
		if allowed[k] {
			out[k] = v
		}
	}
	return out, nil
}

// settle reads the object back and stores it as the new state.
func (r *genericResource) settle(ctx context.Context, id string, prior tftypes.Value, state interface{ setRaw(tftypes.Value) }) error {
	obj, err := r.client.Get(ctx, r.def.Slug, id)
	if err != nil {
		return err
	}
	if obj == nil {
		return fmt.Errorf("%s %s was applied but cannot be read back", r.def.TypeName, id)
	}
	if err := r.checkVariant(obj); err != nil {
		return err
	}
	t, _ := r.stateType(ctx)
	value, err := fromJSON(t, obj, prior, r.paths, "")
	if err != nil {
		return err
	}
	state.setRaw(value)
	return nil
}

func (r *genericResource) checkVariant(obj map[string]any) error {
	if r.def.Discriminator == "" {
		return nil
	}
	if got, _ := obj[r.def.Discriminator].(string); got != r.def.Variant {
		return fmt.Errorf("the object is a %q %s, not %q; use the matching resource type", got, r.def.Slug, r.def.Variant)
	}
	return nil
}

type rawSetter struct{ target *tftypes.Value }

func (s rawSetter) setRaw(v tftypes.Value) { *s.target = v }

func (r *genericResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	body, err := r.body(req.Plan.Raw, false)
	if err != nil {
		resp.Diagnostics.AddError("Cannot build the request", err.Error())
		return
	}
	id, err := uuid.NewV7()
	if err != nil {
		resp.Diagnostics.AddError("Cannot create an id", err.Error())
		return
	}
	if err := r.client.Create(ctx, r.def.Slug, id.String(), body); err != nil {
		resp.Diagnostics.AddError("Creating "+r.def.TypeName+" failed", err.Error())
		return
	}
	if err := r.settle(ctx, id.String(), req.Plan.Raw, rawSetter{&resp.State.Raw}); err != nil {
		resp.Diagnostics.AddError("Reading "+r.def.TypeName+" after create failed", err.Error())
	}
}

func (r *genericResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var id string
	resp.Diagnostics.Append(req.State.GetAttribute(ctx, path.Root("id"), &id)...)
	if resp.Diagnostics.HasError() {
		return
	}
	obj, err := r.client.Get(ctx, r.def.Slug, id)
	if err != nil {
		resp.Diagnostics.AddError("Reading "+r.def.TypeName+" failed", err.Error())
		return
	}
	if obj == nil {
		resp.State.RemoveResource(ctx)
		return
	}
	if err := r.checkVariant(obj); err != nil {
		resp.Diagnostics.AddError("Reading "+r.def.TypeName+" failed", err.Error())
		return
	}
	t, _ := r.stateType(ctx)
	value, err := fromJSON(t, obj, req.State.Raw, r.paths, "")
	if err != nil {
		resp.Diagnostics.AddError("Reading "+r.def.TypeName+" failed", err.Error())
		return
	}
	resp.State.Raw = value
}

func (r *genericResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var id string
	var version int64
	resp.Diagnostics.Append(req.State.GetAttribute(ctx, path.Root("id"), &id)...)
	resp.Diagnostics.Append(req.State.GetAttribute(ctx, path.Root("version"), &version)...)
	if resp.Diagnostics.HasError() {
		return
	}
	body, err := r.body(req.Plan.Raw, true)
	if err != nil {
		resp.Diagnostics.AddError("Cannot build the request", err.Error())
		return
	}
	if err := r.client.Update(ctx, r.def.Slug, id, version, body); err != nil {
		resp.Diagnostics.AddError("Updating "+r.def.TypeName+" failed", err.Error())
		return
	}
	if err := r.settle(ctx, id, req.Plan.Raw, rawSetter{&resp.State.Raw}); err != nil {
		resp.Diagnostics.AddError("Reading "+r.def.TypeName+" after update failed", err.Error())
	}
}

func (r *genericResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var id string
	var version int64
	resp.Diagnostics.Append(req.State.GetAttribute(ctx, path.Root("id"), &id)...)
	resp.Diagnostics.Append(req.State.GetAttribute(ctx, path.Root("version"), &version)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.client.Delete(ctx, r.def.Slug, id, version); err != nil {
		resp.Diagnostics.AddError("Deleting "+r.def.TypeName+" failed", err.Error())
	}
}

func (r *genericResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	if _, err := uuid.Parse(req.ID); err != nil {
		resp.Diagnostics.AddError("Import needs the object's id", "Use the UUID shown by the API or syctl, not the name.")
		return
	}
	obj, err := r.client.Get(ctx, r.def.Slug, req.ID)
	if err != nil {
		resp.Diagnostics.AddError("Importing "+r.def.TypeName+" failed", err.Error())
		return
	}
	if obj == nil {
		resp.Diagnostics.AddError("Nothing to import", fmt.Sprintf("no %s with id %s", r.def.TypeName, req.ID))
		return
	}
	if err := r.checkVariant(obj); err != nil {
		resp.Diagnostics.AddError("Importing "+r.def.TypeName+" failed", err.Error())
		return
	}
	t, _ := r.stateType(ctx)
	value, err := fromJSON(t, obj, tftypes.NewValue(t, nil), r.paths, "")
	if err != nil {
		resp.Diagnostics.AddError("Importing "+r.def.TypeName+" failed", err.Error())
		return
	}
	resp.State.Raw = value
}

// -- data source -------------------------------------------------------------------------------

type genericDataSource struct {
	def    Definition
	paths  jsonPaths
	client *Client
}

func newDataSource(d Definition) func() datasource.DataSource {
	return func() datasource.DataSource {
		return &genericDataSource{def: d, paths: newJSONPaths(d.Attrs)}
	}
}

var (
	_ datasource.DataSource              = (*genericDataSource)(nil)
	_ datasource.DataSourceWithConfigure = (*genericDataSource)(nil)
)

func (d *genericDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_" + d.def.TypeName
}

func (d *genericDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = DataSourceSchema(d.def)
}

func (d *genericDataSource) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
	if client, ok := req.ProviderData.(*Client); ok {
		d.client = client
	}
}

func (d *genericDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var name string
	resp.Diagnostics.Append(req.Config.GetAttribute(ctx, path.Root("name"), &name)...)
	if resp.Diagnostics.HasError() {
		return
	}
	obj, err := d.client.FindByName(ctx, d.def.Slug, name)
	if err != nil {
		resp.Diagnostics.AddError("Looking up "+d.def.TypeName+" failed", err.Error())
		return
	}
	if obj == nil {
		resp.Diagnostics.AddError("Not found", fmt.Sprintf("no %s called %q in this tenant", d.def.TypeName, name))
		return
	}
	if d.def.Discriminator != "" {
		if got, _ := obj[d.def.Discriminator].(string); got != d.def.Variant {
			resp.Diagnostics.AddError("Wrong kind", fmt.Sprintf("%q is a %q %s, not %q", name, got, d.def.Slug, d.def.Variant))
			return
		}
	}
	t := DataSourceSchema(d.def).Type().TerraformType(ctx)
	value, err := fromJSON(t, obj, req.Config.Raw, d.paths, "")
	if err != nil {
		resp.Diagnostics.AddError("Reading "+d.def.TypeName+" failed", err.Error())
		return
	}
	resp.State.Raw = value
}
