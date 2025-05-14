packer {
  required_plugins {
    docker = {
      source  = "github.com/hashicorp/docker"
      version = "~> 1.1"
    }
  }
}

source "docker" "debian" {
  image  = "debian:12"
  commit = true
}

build {
  name = "fleur"
  sources = [
    "source.docker.debian"
  ]

  provisioner "shell" {
    scripts = [
      "install-fleur.sh",
      "clean-apt-files.sh",
    ]
  }

  post-processor "docker-tag" {
    repository = "fleur"
    tags       = ["debian-12"]
  }
}
