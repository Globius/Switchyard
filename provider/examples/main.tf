terraform {
  required_providers {
    switchyard = {
      source = "switchyard/switchyard"
    }
  }
}

# endpoint, token and tenant can also come from SWITCHYARD_ENDPOINT, SWITCHYARD_TOKEN and
# SWITCHYARD_TENANT.
provider "switchyard" {
  endpoint = "https://switchyard.example.test"
  tenant   = "acme"
}

data "switchyard_certificate" "wild" {
  name = "wild-example-test"
}

data "switchyard_se_group" "edge" {
  name = "edge"
}

resource "switchyard_health_monitor" "http" {
  name       = "http-root"
  type       = "http"
  interval_s = 5
  timeout_s  = 2
  rise       = 2
  fall       = 3
  http = {
    path          = "/healthz"
    expect_status = [200]
  }
}

# Members come from Proxmox guests tagged sy-acme.web (ADR 0145); leave members out.
resource "switchyard_pool" "web" {
  name              = "web"
  algorithm         = "leastconn"
  health_monitor_id = switchyard_health_monitor.http.id
  discovery = {
    tag  = "sy-acme.web"
    cidr = "10.20.0.0/24"
    port = 8080
  }
}

resource "switchyard_virtual_service" "web" {
  name            = "web"
  se_group_id     = data.switchyard_se_group.edge.id
  protocol        = "http"
  vip             = "192.0.2.20"
  default_pool_id = switchyard_pool.web.id
  certificate_ids = [data.switchyard_certificate.wild.id]
  ports = [
    { port = 443, tls = true, http2 = true },
    { port = 80, https_redirect_port = 443 },
  ]
}
