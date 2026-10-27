terraform {
  required_version = ">= 1.9"
  required_providers {
    libvirt = {
      source  = "dmacvicar/libvirt"
      version = "= 0.9.9"
    }
  }
}

# Talks to libvirt on the sandbox node through the system ssh client.
provider "libvirt" {
  uri = "qemu+sshcmd://${var.node_user}@${var.node_host}/system?keyfile=${pathexpand(var.ssh_key_file)}"
}
