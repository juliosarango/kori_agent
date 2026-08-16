# Build custom para KoriWebhookFunction (BuildMethod: makefile en template.yaml).
# CodeUri es la raíz del repo, así que aquí controlamos a mano qué entra al
# paquete Lambda: solo lo que lambda_handler.py importa en runtime.
# scripts/, dashboard/, data/, CLAUDE.md, etc. quedan fuera a propósito.

build-KoriWebhookFunction:
	cp lambda_handler.py "$(ARTIFACTS_DIR)/"
	for dir in agents tools hooks; do \
		if [ -d "$$dir" ]; then cp -r "$$dir" "$(ARTIFACTS_DIR)/"; fi \
	done
	if [ -f requirements.txt ]; then \
		pip3 install -r requirements.txt -t "$(ARTIFACTS_DIR)" --no-cache-dir; \
	fi
