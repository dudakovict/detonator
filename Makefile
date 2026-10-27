.DEFAULT_GOAL := help

SBX_HOST ?= 192.168.200.10
SBX_USER ?= sbx
SSH_KEY  ?= ~/.ssh/sbx_host
# Where the repo is synced to on the sandbox node, relative to the user's home.
NODE_DIR ?= detonator

SSH := ssh -i $(SSH_KEY) $(SBX_USER)@$(SBX_HOST)
TF  := terraform -chdir=infra/terraform
# Extra flags for terraform apply/destroy, e.g. TF_ARGS=-auto-approve
TF_ARGS ?=
# The golden image the VMs are built from: whatever sbx-linux-latest points to.
BASE_IMAGE = $$($(SSH) readlink /var/lib/libvirt/images/base/sbx-linux-latest.qcow2)

.PHONY: help ssh node sync image image-test vms vms-destroy snapshot net-test

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*## "} {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

ssh: ## Open a shell on the sandbox node
	$(SSH)

node: ## Configure the sandbox node (KVM, libvirt, Packer) with Ansible
	cd infra/ansible && ansible-playbook node.yml

sync: ## Copy the repo to the sandbox node
	rsync -az --delete --exclude .git/ --exclude packer_cache/ --exclude output/ \
	  --exclude .terraform/ --exclude '*.tfstate*' \
	  -e "ssh -i $(SSH_KEY)" ./ $(SBX_USER)@$(SBX_HOST):$(NODE_DIR)/

image: sync ## Build the guest image with Packer on the sandbox node
	$(SSH) $(NODE_DIR)/infra/packer/linux-guest/build.sh

image-test: sync ## Boot the latest guest image once and exercise the agent
	$(SSH) $(NODE_DIR)/infra/packer/linux-guest/smoke-test.sh

vms: ## Create the isolated network and sandbox VMs from the latest image (Terraform)
	$(TF) init -input=false
	$(TF) apply $(TF_ARGS) -var "base_image=$(BASE_IMAGE)"

vms-destroy: ## Remove the sandbox VMs and network
	$(SSH) 'for vm in $$(virsh -c qemu:///system list --all --name | grep ^sbx-linux-); do \
	  virsh -c qemu:///system snapshot-delete $$vm clean 2>/dev/null; done; true'
	$(TF) destroy $(TF_ARGS) -var "base_image=$(BASE_IMAGE)"

snapshot: sync ## Take the "clean" snapshot of every sandbox VM
	$(SSH) $(NODE_DIR)/scripts/snapshot.sh

net-test: sync ## Prove from inside a VM that the network is isolated and sinkholed
	$(SSH) $(NODE_DIR)/scripts/net-test.sh
