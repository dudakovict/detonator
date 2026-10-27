variable "node_host" {
  type    = string
  default = "192.168.200.10"
}

variable "node_user" {
  type    = string
  default = "sbx"
}

variable "ssh_key_file" {
  type    = string
  default = "~/.ssh/sbx_host"
}

variable "base_image" {
  type        = string
  description = "Golden image file name in /var/lib/libvirt/images/base, e.g. sbx-linux-20261027-1248.qcow2. Changing it replaces every VM."
}

variable "vm_count" {
  type    = number
  default = 2
}

variable "vm_memory_mib" {
  type    = number
  default = 768
}
