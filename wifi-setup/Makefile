.PHONY: help import link reload unlink

help:
	@echo "Targets:"
	@echo "  import   - Copy current system files into the repo (add --force to overwrite)."
	@echo "  link     - Symlink repo files into system locations (requires sudo)."
	@echo "  reload   - systemctl daemon-reload and restart relevant services (requires sudo)."
	@echo "  unlink   - Remove symlinks and restore latest .bak backups (requires sudo)."

import:
	./scripts/import-from-system.sh $(ARGS)

link:
	sudo -E ./scripts/link.sh

reload:
	sudo -E ./scripts/reload-services.sh

unlink:
	sudo -E ./scripts/unlink.sh

