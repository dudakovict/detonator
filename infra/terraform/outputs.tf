output "vms" {
  description = "Sandbox VMs and their fixed addresses on the isolated network."
  value       = { for name, vm in local.vms : name => vm.ip }
}
