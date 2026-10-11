# Builds the SE template VM on Proxmox: boots the Debian 13 cloud image with cloud-init/template-build.yaml,
# which runs provision.sh and powers off. Then convert the VM to a template (see
# docs/runbooks/build-se-template.md) and set its vmid as the SE group's template_vmid.
terraform {
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.66"
    }
  }
}

variable "endpoint" { type = string }
variable "api_token" {
  type      = string
  sensitive = true
}
variable "node" { type = string }
variable "datastore" {
  type    = string
  default = "local-lvm"
}
variable "snippets_datastore" {
  type    = string
  default = "local"
}
variable "bridge" {
  type    = string
  default = "vmbr0"
}
variable "vmid" {
  type    = number
  default = 9000
}
variable "debian_image_url" {
  type    = string
  default = "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2"
}
variable "user_data_file" {
  type        = string
  description = "Rendered cloud-init/template-build.yaml, with sy-agent and se-image/ already copied to the VM (see runbook)."
}

provider "proxmox" {
  endpoint  = var.endpoint
  api_token = var.api_token
}

resource "proxmox_virtual_environment_download_file" "debian" {
  content_type = "iso"
  datastore_id = var.snippets_datastore
  node_name    = var.node
  url          = var.debian_image_url
  file_name    = "debian-13-generic-amd64.img"
}

resource "proxmox_virtual_environment_file" "user_data" {
  content_type = "snippets"
  datastore_id = var.snippets_datastore
  node_name    = var.node
  source_file {
    path = var.user_data_file
  }
}

resource "proxmox_virtual_environment_vm" "se_template_build" {
  name      = "sy-se-template-build"
  node_name = var.node
  vm_id     = var.vmid
  agent { enabled = true }
  cpu {
    cores = 2
    type  = "host"
  }
  memory { dedicated = 2048 }
  disk {
    datastore_id = var.datastore
    file_id      = proxmox_virtual_environment_download_file.debian.id
    interface    = "scsi0"
    size         = 10
  }
  network_device { bridge = var.bridge }
  initialization {
    datastore_id      = var.datastore
    user_data_file_id = proxmox_virtual_environment_file.user_data.id
    ip_config {
      ipv4 { address = "dhcp" }
    }
  }
}
