locals {
  cidr       = "10.66.0.0/24"
  base_dir   = "/var/lib/libvirt/images/base"
  disk_bytes = 8 * 1024 * 1024 * 1024

  # sbx-linux-01 → 10.66.0.11, sbx-linux-02 → 10.66.0.12, … with matching MACs,
  # so every VM always gets the same address.
  vms = {
    for i in range(var.vm_count) : format("sbx-linux-%02d", i + 1) => {
      ip  = cidrhost(local.cidr, 11 + i)
      mac = format("52:54:00:66:00:%02x", 11 + i)
    }
  }
}

resource "libvirt_pool" "sbx" {
  name = "sbx"
  type = "dir"
  target = {
    path = "/var/lib/libvirt/images/sbx"
  }
}

# Isolated: no forward block, so libvirt routes nothing off this network.
# libvirt only serves DHCP; DNS and HTTP are answered by the sinkhole on .1.
resource "libvirt_network" "isolated" {
  name      = "sbx-isolated"
  autostart = true
  bridge = {
    name = "virbr-sbx"
    # A snapshot revert re-plugs the VM's tap device; with STP on, the new port
    # waits ~4 s before forwarding. One host-only bridge can't have loops.
    stp = "off"
  }
  dns = {
    enable = "no"
  }
  ips = [{
    address = cidrhost(local.cidr, 1)
    prefix  = 24
    dhcp = {
      hosts = [for name, vm in local.vms : { name = name, mac = vm.mac, ip = vm.ip }]
    }
  }]
}

# Each VM's disk is a thin overlay on the read-only golden image.
resource "libvirt_volume" "disk" {
  for_each = local.vms

  name     = "${each.key}.qcow2"
  pool     = libvirt_pool.sbx.name
  capacity = local.disk_bytes
  target = {
    format = { type = "qcow2" }
  }
  backing_store = {
    path   = "${local.base_dir}/${var.base_image}"
    format = { type = "qcow2" }
  }
}

resource "libvirt_domain" "vm" {
  for_each = local.vms

  name        = each.key
  type        = "kvm"
  vcpu        = 1
  memory      = var.vm_memory_mib
  memory_unit = "MiB"
  running     = true

  cpu = {
    mode = "host-passthrough"
  }

  os = {
    type         = "hvm"
    type_arch    = "x86_64"
    type_machine = "q35"
    boot_devices = [{ dev = "hd" }]
  }

  devices = {
    disks = [{
      source = {
        file = { file = libvirt_volume.disk[each.key].path }
      }
      driver = { name = "qemu", type = "qcow2" }
      target = { dev = "vda", bus = "virtio" }
    }]
    interfaces = [{
      mac    = { address = each.value.mac }
      model  = { type = "virtio" }
      source = { network = { network = libvirt_network.isolated.name } }
    }]
    # Serial console for debugging: virsh console sbx-linux-01
    consoles = [{
      target = { type = "serial", port = 0 }
    }]
  }
}
