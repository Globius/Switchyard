// Command terraform-provider-switchyard serves the Switchyard provider to OpenTofu and Terraform.
package main

import (
	"context"
	"flag"
	"log"

	"github.com/hashicorp/terraform-plugin-framework/providerserver"

	"switchyard/provider/internal/provider"
)

// version is set at build time: -ldflags "-X main.version=1.2.3".
var version = "dev"

func main() {
	debug := flag.Bool("debug", false, "serve with debugger support")
	flag.Parse()
	err := providerserver.Serve(context.Background(), provider.New(version), providerserver.ServeOpts{
		Address: "registry.opentofu.org/switchyard/switchyard",
		Debug:   *debug,
	})
	if err != nil {
		log.Fatal(err)
	}
}
