# Образы стенда: `docker buildx bake` (или `make images`). Push делает человек:
# `docker buildx bake --push`. Теги совпадают с docker-compose.yml.
# Веса берутся из локального backend/models собирающего — `make local-models`.

group "default" {
  targets = ["backend", "frontend", "llm", "sip"]
}

target "_common" {
  platforms = ["linux/amd64"]
}

target "backend" {
  inherits   = ["_common"]
  context    = "backend"
  target     = "voice"
  contexts = {
    models    = "backend/models"
    scenarios = "scenarios"
    licenses  = "deploy/licenses"
  }
  tags = ["registry.gglamer.ru/lct-hack-backend:2026.09.29"]
}

target "frontend" {
  inherits = ["_common"]
  context  = "frontend"
  tags     = ["registry.gglamer.ru/lct-hack-frontend:2026.09.29"]
}

target "llm" {
  inherits = ["_common"]
  context  = "deploy/llm"
  contexts = {
    models   = "backend/models"
    licenses = "deploy/licenses"
  }
  tags = ["registry.gglamer.ru/lct-hack-llm:2026.09.29"]
}

target "sip" {
  inherits = ["_common"]
  context  = "sip"
  tags     = ["registry.gglamer.ru/lct-hack-sip:2026.09.29"]
}
