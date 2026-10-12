// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

// Package provider is the OpenTofu and Terraform provider for the Switchyard controller.
//
// The resource schemas are generated from the controller's OpenAPI document (see attr.go); the
// behaviour of every resource is the same generic code in resource.go.
package provider

import (
	"context"
	"os"
	"time"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/provider"
	"github.com/hashicorp/terraform-plugin-framework/provider/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// New returns the provider factory.
func New(version string) func() provider.Provider {
	return func() provider.Provider { return &switchyardProvider{version: version} }
}

type switchyardProvider struct{ version string }

type providerModel struct {
	Endpoint     types.String `tfsdk:"endpoint"`
	Token        types.String `tfsdk:"token"`
	Tenant       types.String `tfsdk:"tenant"`
	CAFile       types.String `tfsdk:"ca_file"`
	Insecure     types.Bool   `tfsdk:"insecure_skip_verify"`
	ApplyTimeout types.String `tfsdk:"apply_timeout"`
}

func (p *switchyardProvider) Metadata(_ context.Context, _ provider.MetadataRequest, resp *provider.MetadataResponse) {
	resp.TypeName = "switchyard"
	resp.Version = p.version
}

func (p *switchyardProvider) Schema(_ context.Context, _ provider.SchemaRequest, resp *provider.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manages the desired state of one Switchyard tenant. Every write is applied through " +
			"the controller's change pipeline (validation, canary, revision), one object at a time.",
		Attributes: map[string]schema.Attribute{
			"endpoint": schema.StringAttribute{
				Optional:    true,
				Description: "Controller URL, for example https://switchyard.example.test. Environment: SWITCHYARD_ENDPOINT.",
			},
			"token": schema.StringAttribute{
				Optional:    true,
				Sensitive:   true,
				Description: "API token (sy_...) of a user with an operator role in the tenant. Environment: SWITCHYARD_TOKEN.",
			},
			"tenant": schema.StringAttribute{
				Optional:    true,
				Description: "Tenant name or id. Environment: SWITCHYARD_TENANT.",
			},
			"ca_file": schema.StringAttribute{
				Optional:    true,
				Description: "PEM file with the CA that signed the controller's certificate. Environment: SWITCHYARD_CA_FILE.",
			},
			"insecure_skip_verify": schema.BoolAttribute{
				Optional:    true,
				Description: "Do not verify the controller's certificate. Labs only.",
			},
			"apply_timeout": schema.StringAttribute{
				Optional:    true,
				Description: "How long to wait for one change to apply, as a Go duration. Default 10m.",
			},
		},
	}
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}

func (p *switchyardProvider) Configure(ctx context.Context, req provider.ConfigureRequest, resp *provider.ConfigureResponse) {
	var cfg providerModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &cfg)...)
	if resp.Diagnostics.HasError() {
		return
	}
	timeout := 10 * time.Minute
	if v := cfg.ApplyTimeout.ValueString(); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil || d <= 0 {
			resp.Diagnostics.AddError("Invalid apply_timeout", "Use a positive Go duration such as 10m.")
			return
		}
		timeout = d
	}
	client, err := NewClient(ClientConfig{
		Endpoint:     firstNonEmpty(cfg.Endpoint.ValueString(), os.Getenv("SWITCHYARD_ENDPOINT")),
		Token:        firstNonEmpty(cfg.Token.ValueString(), os.Getenv("SWITCHYARD_TOKEN")),
		Tenant:       firstNonEmpty(cfg.Tenant.ValueString(), os.Getenv("SWITCHYARD_TENANT")),
		CAFile:       firstNonEmpty(cfg.CAFile.ValueString(), os.Getenv("SWITCHYARD_CA_FILE")),
		Insecure:     cfg.Insecure.ValueBool(),
		ApplyTimeout: timeout,
	})
	if err != nil {
		resp.Diagnostics.AddError("Cannot configure the Switchyard provider", err.Error())
		return
	}
	resp.ResourceData = client
	resp.DataSourceData = client
}

func (p *switchyardProvider) Resources(context.Context) []func() resource.Resource {
	var out []func() resource.Resource
	for _, d := range Definitions {
		if d.Resource {
			out = append(out, newResource(d))
		}
	}
	return out
}

func (p *switchyardProvider) DataSources(context.Context) []func() datasource.DataSource {
	var out []func() datasource.DataSource
	for _, d := range Definitions {
		out = append(out, newDataSource(d))
	}
	return out
}
