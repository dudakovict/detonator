packer {
  required_version = ">= 1.16.0"
  required_plugins {
    qemu = {
      source  = "github.com/hashicorp/qemu"
      version = "= 1.1.7"
    }
    ansible = {
      source  = "github.com/hashicorp/ansible"
      version = "= 1.1.6"
    }
  }
}

variable "ubuntu_release" {
  type        = string
  default     = "20261017"
  description = "Ubuntu 24.04 minimal cloud image release (a dated, immutable build)."
}

variable "ubuntu_image_sha256" {
  type    = string
  default = "b3296484e362ba2e713489a5de6225c719d9500962bde25d15f2f21e512c7658"
}

variable "ssh_key_file" {
  type        = string
  description = "Throwaway private key for the build; its .pub is injected via cloud-init."
}

locals {
  version = formatdate("YYYYMMDD-hhmm", timestamp())
  name    = "sbx-linux-${local.version}"
}

source "qemu" "ubuntu" {
  qemu_binary  = "/usr/libexec/qemu-kvm"
  accelerator  = "kvm"
  machine_type = "q35"
  cpu_model    = "host"
  cpus         = 2
  memory       = 512 # the node runs ELK and two sandbox VMs too; see build.sh
  headless     = true

  iso_url        = "https://cloud-images.ubuntu.com/minimal/releases/noble/release-${var.ubuntu_release}/ubuntu-24.04-minimal-cloudimg-amd64.img"
  iso_checksum   = "sha256:${var.ubuntu_image_sha256}"
  disk_image     = true
  disk_size      = "8G"
  disk_interface = "virtio"
  net_device     = "virtio-net"

  format           = "qcow2"
  disk_compression = true
  output_directory = "output/${local.name}"
  vm_name          = "${local.name}.qcow2"

  # cloud-init seed served over HTTP by Packer. The SMBIOS serial tells cloud-init
  # where to fetch it (10.0.2.2 is the host, seen from QEMU's user network). A seed
  # CD-ROM doesn't work here: the minimal image's kernel can't read q35's SATA CD.
  http_content = {
    "/meta-data" = "instance-id: ${local.name}\nlocal-hostname: ubuntu\n"
    "/user-data" = templatefile("${path.root}/user-data.pkrtpl", {
      ssh_public_key = trimspace(file("${var.ssh_key_file}.pub"))
    })
  }
  qemuargs = [
    ["-smbios", "type=1,serial=ds=nocloud;s=http://{{ .HTTPIP }}:{{ .HTTPPort }}/"],
  ]

  communicator         = "ssh"
  ssh_username         = "packer"
  ssh_private_key_file = var.ssh_key_file
  ssh_timeout          = "10m"
  # Remove the build user as the very last step, then power off.
  shutdown_command = "sudo sh -c 'userdel -f -r packer; rm -f /etc/sudoers.d/90-cloud-init-users; shutdown -P now'"
}

build {
  sources = ["source.qemu.ubuntu"]

  provisioner "ansible" {
    command       = "/opt/ansible/bin/ansible-playbook"
    playbook_file = "${path.root}/../../ansible/guest.yml"
    user          = "packer"
    use_proxy     = false
  }
}
