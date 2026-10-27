.DEFAULT_GOAL := help

SBX_HOST ?= 192.168.200.10
SBX_USER ?= sbx
SSH_KEY  ?= ~/.ssh/sbx_host
# Where the repo is synced to on the sandbox node, relative to the user's home.
NODE_DIR ?= detonator

SSH := ssh -i $(SSH_KEY) $(SBX_USER)@$(SBX_HOST)

.PHONY: help ssh node sync image image-test

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*## "} {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

ssh: ## Open a shell on the sandbox node
	$(SSH)

node: ## Configure the sandbox node (KVM, libvirt, Packer) with Ansible
	cd infra/ansible && ansible-playbook node.yml

sync: ## Copy the repo to the sandbox node
	rsync -az --delete --exclude .git/ --exclude packer_cache/ --exclude output/ \
	  -e "ssh -i $(SSH_KEY)" ./ $(SBX_USER)@$(SBX_HOST):$(NODE_DIR)/

image: sync ## Build the guest image with Packer on the sandbox node
	$(SSH) $(NODE_DIR)/infra/packer/linux-guest/build.sh

image-test: sync ## Boot the latest guest image once and exercise the agent
	$(SSH) $(NODE_DIR)/infra/packer/linux-guest/smoke-test.sh
