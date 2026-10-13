// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

package provider

import (
	"context"
	"encoding/json"
	"fmt"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/tfsdk"
	"github.com/hashicorp/terraform-plugin-go/tftypes"
)

// fakeController is just enough of the controller API: objects in memory, every write applied by
// a job that has already succeeded.
type fakeController struct {
	mu       sync.Mutex
	objects  map[string]map[string]any // slug/id -> object
	jobs     map[string]map[string]any
	requests []string
	headers  []http.Header
	bodies   []map[string]any
	// defaults filled in on create, like the controller does
	defaults map[string]map[string]any
	// rejectNext makes the next write's job finish with ok=false
	rejectNext bool
}

func newFake() *fakeController {
	return &fakeController{
		objects: map[string]map[string]any{},
		jobs:    map[string]map[string]any{},
		defaults: map[string]map[string]any{
			"pools": {"algorithm": "roundrobin", "tls": false, "tls_verify": false, "members": []any{}},
		},
	}
}

func (f *fakeController) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.requests = append(f.requests, r.Method+" "+r.URL.RequestURI())
	f.headers = append(f.headers, r.Header.Clone())
	if r.Header.Get("Authorization") != "Bearer sy_test" {
		http.Error(w, `{"title":"unauthorized"}`, http.StatusUnauthorized)
		return
	}
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/api/v1/tenants/"), "/")
	if len(parts) < 2 || parts[0] != "acme" {
		http.NotFound(w, r)
		return
	}
	slug := parts[1]
	if slug == "jobs" {
		_ = json.NewEncoder(w).Encode(f.jobs[parts[2]])
		return
	}
	var body map[string]any
	if r.Body != nil {
		_ = json.NewDecoder(r.Body).Decode(&body)
	}
	f.bodies = append(f.bodies, body)
	switch {
	case r.Method == http.MethodGet && len(parts) == 2:
		items := []any{}
		for key, o := range f.objects {
			if strings.HasPrefix(key, slug+"/") {
				items = append(items, o)
			}
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"items": items, "next_cursor": nil})
	case r.Method == http.MethodGet:
		o, ok := f.objects[slug+"/"+parts[2]]
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"title":"not found"}`))
			return
		}
		_ = json.NewEncoder(w).Encode(o)
	case r.Method == http.MethodPost:
		id := r.URL.Query().Get("object_id")
		o := map[string]any{"id": id, "version": 1}
		for k, v := range f.defaults[slug] {
			o[k] = v
		}
		for k, v := range body {
			o[k] = v
		}
		f.objects[slug+"/"+id] = o
		f.accept(w, id)
	case r.Method == http.MethodPatch:
		o := f.objects[slug+"/"+parts[2]]
		if r.Header.Get("If-Match") != fmt.Sprintf(`"%v"`, o["version"]) {
			w.WriteHeader(http.StatusPreconditionFailed)
			_, _ = w.Write([]byte(`{"title":"stale version"}`))
			return
		}
		for k, v := range body {
			o[k] = v
		}
		o["version"] = o["version"].(int) + 1
		f.accept(w, parts[2])
	case r.Method == http.MethodDelete:
		delete(f.objects, slug+"/"+parts[2])
		f.accept(w, parts[2])
	}
}

func (f *fakeController) accept(w http.ResponseWriter, id string) {
	job := fmt.Sprintf("job-%d", len(f.jobs)+1)
	state := map[string]any{"id": job, "state": "succeeded", "ok": true}
	if f.rejectNext {
		f.rejectNext = false
		state["ok"] = false
		state["result"] = map[string]any{"error": "SE group edge: canary failed"}
	}
	f.jobs[job] = state
	w.WriteHeader(http.StatusAccepted)
	_ = json.NewEncoder(w).Encode(map[string]any{"change_set_id": "cs-1", "object_id": id, "job_id": job})
}

func (f *fakeController) client(t *testing.T) *Client {
	t.Helper()
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)
	c, err := NewClient(ClientConfig{Endpoint: srv.URL, Token: "sy_test", Tenant: "acme", PollInterval: time.Millisecond, ApplyTimeout: 5 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func definition(t *testing.T, name string) Definition {
	t.Helper()
	for _, d := range Definitions {
		if d.TypeName == name {
			return d
		}
	}
	t.Fatalf("no definition %s", name)
	return Definition{}
}

// plan builds the value Terraform would plan: what config gives, unknown where the controller fills in.
func plan(t tftypes.Type, config any) tftypes.Value {
	if config == nil {
		return tftypes.NewValue(t, tftypes.UnknownValue)
	}
	switch tt := t.(type) {
	case tftypes.Object:
		cfg, _ := config.(map[string]any)
		values := map[string]tftypes.Value{}
		for name, at := range tt.AttributeTypes {
			values[name] = plan(at, cfg[name])
		}
		return tftypes.NewValue(t, values)
	case tftypes.List:
		items, _ := config.([]any)
		values := []tftypes.Value{}
		for _, item := range items {
			values = append(values, plan(tt.ElementType, item))
		}
		return tftypes.NewValue(t, values)
	}
	switch v := config.(type) {
	case int:
		return tftypes.NewValue(t, int64(v))
	}
	return tftypes.NewValue(t, config)
}

func newPoolResource(t *testing.T, c *Client) (*genericResource, tftypes.Type) {
	t.Helper()
	d := definition(t, "pool")
	r := &genericResource{def: d, paths: newJSONPaths(d.Attrs), client: c}
	return r, ResourceSchema(d).Type().TerraformType(context.Background())
}

func field(t *testing.T, v tftypes.Value, name string) tftypes.Value {
	t.Helper()
	var fields map[string]tftypes.Value
	if err := v.As(&fields); err != nil {
		t.Fatal(err)
	}
	return fields[name]
}

func str(t *testing.T, v tftypes.Value) string {
	t.Helper()
	var s string
	if err := v.As(&s); err != nil {
		t.Fatalf("not a string: %v (%s)", err, v)
	}
	return s
}

func TestEverySchemaIsValidForTheFramework(t *testing.T) {
	ctx := context.Background()
	for _, d := range Definitions {
		if d.Resource {
			if diags := ResourceSchema(d).ValidateImplementation(ctx); diags.HasError() {
				t.Errorf("resource %s: %v", d.TypeName, diags)
			}
		}
		if diags := DataSourceSchema(d).ValidateImplementation(ctx); diags.HasError() {
			t.Errorf("data source %s: %v", d.TypeName, diags)
		}
	}
}

func TestTheGeneratedTableIsCurrent(t *testing.T) {
	out, err := exec.Command("go", "run", "../gen", "-spec", "../../../docs/api/openapi.json", "-out", "zz_generated.go", "-check").CombinedOutput()
	if err != nil {
		t.Fatalf("zz_generated.go is stale; run `go generate ./...` in provider/: %v\n%s", err, out)
	}
}

func TestEveryKindOfTheAPIIsCovered(t *testing.T) {
	want := []string{"network", "vip_pool", "se_group", "virtual_service", "pool", "health_monitor", "certificate",
		"allow_list_policy_set", "redirect_policy_set", "bgp_routing_profile", "vrrp_routing_profile"}
	have := map[string]Definition{}
	for _, d := range Definitions {
		have[d.TypeName] = d
	}
	for _, name := range want {
		if _, ok := have[name]; !ok {
			t.Errorf("no definition for %s", name)
		}
	}
	if have["certificate"].Resource {
		t.Error("certificates carry key material in OpenBao and must stay a data source")
	}
	if got := have["redirect_policy_set"].Consts["kind"]; got != "redirect" {
		t.Errorf("discriminator const = %q", got)
	}
}

func TestCreateReadUpdateDelete(t *testing.T) {
	ctx := context.Background()
	fake := newFake()
	r, typ := newPoolResource(t, fake.client(t))
	schema := ResourceSchema(r.def)

	// create: the plan leaves the id, version and defaults unknown
	planned := plan(typ, map[string]any{
		"name":    "web",
		"members": []any{map[string]any{"address": "10.20.0.11", "port": 8080}},
	})
	createResp := resource.CreateResponse{State: tfsdk.State{Schema: schema, Raw: tftypes.NewValue(typ, nil)}}
	r.Create(ctx, resource.CreateRequest{Plan: tfsdk.Plan{Schema: schema, Raw: planned}}, &createResp)
	if createResp.Diagnostics.HasError() {
		t.Fatal(createResp.Diagnostics)
	}
	post := fake.bodies[0]
	if _, has := post["id"]; has {
		t.Error("id must not be sent")
	}
	if _, has := post["algorithm"]; has {
		t.Error("unknown (defaulted) attributes must not be sent")
	}
	if !strings.HasPrefix(fake.requests[0], "POST /api/v1/tenants/acme/pools?") || !strings.Contains(fake.requests[0], "apply=true") {
		t.Errorf("create request = %s", fake.requests[0])
	}
	state := createResp.State.Raw
	id := str(t, field(t, state, "id"))
	if id == "" || !strings.Contains(fake.requests[0], "object_id="+id) {
		t.Errorf("the id %q must be the object_id of the create", id)
	}
	if got := str(t, field(t, state, "algorithm")); got != "roundrobin" {
		t.Errorf("algorithm = %q, want the controller's default", got)
	}

	// update: only the changed field, with the version as If-Match
	planned = plan(typ, map[string]any{"name": "web", "algorithm": "leastconn", "id": id, "version": 1,
		"members": []any{map[string]any{"address": "10.20.0.11", "port": 8080}}})
	updateResp := resource.UpdateResponse{State: tfsdk.State{Schema: schema, Raw: state}}
	r.Update(ctx, resource.UpdateRequest{Plan: tfsdk.Plan{Schema: schema, Raw: planned}, State: tfsdk.State{Schema: schema, Raw: state}}, &updateResp)
	if updateResp.Diagnostics.HasError() {
		t.Fatal(updateResp.Diagnostics)
	}
	var patchHeader http.Header
	for i, req := range fake.requests {
		if strings.HasPrefix(req, "PATCH ") {
			patchHeader = fake.headers[i]
		}
	}
	if patchHeader.Get("If-Match") != `"1"` {
		t.Errorf("If-Match = %q", patchHeader.Get("If-Match"))
	}
	var version big.Float
	if err := field(t, updateResp.State.Raw, "version").As(&version); err != nil {
		t.Fatal(err)
	}
	if v, _ := version.Int64(); v != 2 {
		t.Errorf("version = %d, want 2", v)
	}
	if got := str(t, field(t, updateResp.State.Raw, "algorithm")); got != "leastconn" {
		t.Errorf("algorithm after update = %q", got)
	}

	// read: an object deleted behind Terraform's back leaves the state
	delete(fake.objects, "pools/"+id)
	readResp := resource.ReadResponse{State: tfsdk.State{Schema: schema, Raw: updateResp.State.Raw}}
	r.Read(ctx, resource.ReadRequest{State: tfsdk.State{Schema: schema, Raw: updateResp.State.Raw}}, &readResp)
	if !readResp.State.Raw.IsNull() {
		t.Error("a vanished object must be removed from the state")
	}

	// delete of an object that is gone is not an error
	delResp := resource.DeleteResponse{}
	r.Delete(ctx, resource.DeleteRequest{State: tfsdk.State{Schema: schema, Raw: updateResp.State.Raw}}, &delResp)
	if delResp.Diagnostics.HasError() {
		t.Error(delResp.Diagnostics)
	}
}

func TestARevertedChangeIsAnErrorWithTheReason(t *testing.T) {
	fake := newFake()
	fake.rejectNext = true
	r, typ := newPoolResource(t, fake.client(t))
	schema := ResourceSchema(r.def)
	resp := resource.CreateResponse{State: tfsdk.State{Schema: schema, Raw: tftypes.NewValue(typ, nil)}}
	r.Create(context.Background(), resource.CreateRequest{Plan: tfsdk.Plan{Schema: schema, Raw: plan(typ, map[string]any{"name": "web"})}}, &resp)
	if !resp.Diagnostics.HasError() || !strings.Contains(resp.Diagnostics.Errors()[0].Detail(), "canary failed") {
		t.Fatalf("diagnostics = %v", resp.Diagnostics)
	}
}

func TestAServerProblemListsTheFieldErrors(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"title":"Validation failed","detail":"2 problems","errors":[{"path":"members.0.port","code":"x","message":"port out of range"}]}`))
	}))
	defer srv.Close()
	c, _ := NewClient(ClientConfig{Endpoint: srv.URL, Token: "t", Tenant: "acme"})
	err := c.Create(context.Background(), "pools", "id", map[string]any{})
	if err == nil || !strings.Contains(err.Error(), "members.0.port: port out of range") || !strings.Contains(err.Error(), "422") {
		t.Fatalf("err = %v", err)
	}
}

func TestADataSourceFindsAnObjectByName(t *testing.T) {
	fake := newFake()
	fake.objects["pools/p1"] = map[string]any{"id": "p1", "version": 3, "name": "web", "algorithm": "leastconn"}
	fake.objects["pools/p2"] = map[string]any{"id": "p2", "version": 1, "name": "api", "algorithm": "roundrobin"}
	d := &genericDataSource{def: definition(t, "pool"), client: fake.client(t)}
	d.paths = newJSONPaths(d.def.Attrs)
	schema := DataSourceSchema(d.def)
	typ := schema.Type().TerraformType(context.Background())
	config := plan(typ, map[string]any{"name": "web"})
	resp := datasource.ReadResponse{State: tfsdk.State{Schema: schema, Raw: tftypes.NewValue(typ, nil)}}
	d.Read(context.Background(), datasource.ReadRequest{Config: tfsdk.Config{Schema: schema, Raw: config}}, &resp)
	if resp.Diagnostics.HasError() {
		t.Fatal(resp.Diagnostics)
	}
	if got := str(t, field(t, resp.State.Raw, "id")); got != "p1" {
		t.Errorf("id = %q", got)
	}

	config = plan(typ, map[string]any{"name": "missing"})
	resp = datasource.ReadResponse{State: tfsdk.State{Schema: schema, Raw: tftypes.NewValue(typ, nil)}}
	d.Read(context.Background(), datasource.ReadRequest{Config: tfsdk.Config{Schema: schema, Raw: config}}, &resp)
	if !resp.Diagnostics.HasError() {
		t.Error("a missing name must be an error")
	}
}

func TestAnObjectOfAnotherVariantIsRefused(t *testing.T) {
	fake := newFake()
	fake.objects["policy-sets/x"] = map[string]any{"id": "x", "version": 1, "name": "n", "kind": "allow_list", "cidrs": []any{}}
	d := definition(t, "redirect_policy_set")
	r := &genericResource{def: d, paths: newJSONPaths(d.Attrs), client: fake.client(t)}
	schema := ResourceSchema(d)
	typ := schema.Type().TerraformType(context.Background())
	resp := resource.ImportStateResponse{State: tfsdk.State{Schema: schema, Raw: tftypes.NewValue(typ, nil)}}
	r.ImportState(context.Background(), resource.ImportStateRequest{ID: "0192f3a0-0000-7000-8000-000000000001"}, &resp)
	if !resp.Diagnostics.HasError() {
		t.Error("importing by a bad id must fail")
	}
	fake.objects["policy-sets/0192f3a0-0000-7000-8000-000000000001"] = fake.objects["policy-sets/x"]
	resp = resource.ImportStateResponse{State: tfsdk.State{Schema: schema, Raw: tftypes.NewValue(typ, nil)}}
	r.ImportState(context.Background(), resource.ImportStateRequest{ID: "0192f3a0-0000-7000-8000-000000000001"}, &resp)
	if !resp.Diagnostics.HasError() || !strings.Contains(resp.Diagnostics.Errors()[0].Detail(), "not \"redirect\"") {
		t.Fatalf("diagnostics = %v", resp.Diagnostics)
	}
}

func TestClientConfigIsChecked(t *testing.T) {
	for name, cfg := range map[string]ClientConfig{
		"no scheme":   {Endpoint: "switchyard.example.test", Token: "t", Tenant: "a"},
		"credentials": {Endpoint: "https://u:p@switchyard.example.test", Token: "t", Tenant: "a"},
		"no token":    {Endpoint: "https://switchyard.example.test", Tenant: "a"},
		"no tenant":   {Endpoint: "https://switchyard.example.test", Token: "t"},
		"bad ca":      {Endpoint: "https://switchyard.example.test", Token: "t", Tenant: "a", CAFile: "does-not-exist.pem"},
	} {
		if _, err := NewClient(cfg); err == nil {
			t.Errorf("%s: want an error", name)
		}
	}
}
