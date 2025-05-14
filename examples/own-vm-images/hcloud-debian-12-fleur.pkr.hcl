packer {
  required_plugins {
    hcloud = {
      source  = "github.com/hetznercloud/hcloud"
      version = "~> 1.6"
    }
  }
}

variable "hcloud_token" {
  type      = string
  sensitive = true
  default   = "${env("HCLOUD_TOKEN")}"
}

locals {
  current_timestamp = "${formatdate("YYYY-MM-DD-hhmm", timestamp())}Z"
}

source "hcloud" "debian" {
  token = var.hcloud_token

  image       = "debian-12"
  location    = "fsn1"
  server_type = "ccx13" # you need at least 8 Gb of RAM
  server_name = "fleur-build-${local.current_timestamp}"

  # keep disk small
  user_data = <<-EOF
    #cloud-config
    growpart:
      mode: "off"
    resize_rootfs: false
  EOF

  ssh_username = "root"

  snapshot_name = "fleur-debian-${local.current_timestamp}"
}

build {
  sources = ["source.hcloud.debian"]

  provisioner "shell" {
    inline           = [
      "cloud-init status --wait --long"
    ]
    valid_exit_codes = [0, 2]
  }

  provisioner "shell" {
    inline = [
      "apt-get update",
    ]
  }

  provisioner "shell" {
    scripts = [
      "install-fleur.sh",
      "reset-cloud-init.sh",
      "reset-ssh-host-keys.sh",
      "clean-apt-files.sh",
      "clean-logs.sh",
      "clean-root.sh",
      "flush-disk.sh",
    ]
  }
}
