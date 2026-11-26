# Skills del Proyecto — Monitoreo Climático Forestal (INAB)

Colección curada de 11 skills especializados extraídos de [agentic-awesome-skills](https://github.com/sickn33/agentic-awesome-skills), integrados directamente en el repositorio para tareas de análisis satelital, automatización de datos, frontend cartográfico y seguridad.

---

## 🛰️ 1. Teledetección y Datos Meteorológicos

| Skill | Directorio | Descripción |
| :--- | :--- | :--- |
| **`radar-satellite-analysis`** | [`skills/radar-satellite-analysis/`](skills/radar-satellite-analysis/SKILL.md) | Validación e interpretación de metadatos satelitales, cobertura de nubes, bandas espectrales y geometrías. |
| **`noaa-radar-satellite-fetching`** | [`skills/noaa-radar-satellite-fetching/`](skills/noaa-radar-satellite-fetching/SKILL.md) | Recuperación resiliente de observaciones satelitales/meteorológicas desde repositorios en la nube. |
| **`nexrad-radar-visualization`** | [`skills/nexrad-radar-visualization/`](skills/nexrad-radar-visualization/SKILL.md) | Mapeo y proyección de matrices climáticas, grids, máscaras de calidad y escalas de color. |

---

## ⚙️ 2. Pipelines de Datos y Python

| Skill | Directorio | Descripción |
| :--- | :--- | :--- |
| **`data-engineering-data-pipeline`** | [`skills/data-engineering-data-pipeline/`](skills/data-engineering-data-pipeline/SKILL.md) | Arquitectura y mejores prácticas para pipelines ETL tolerantes a fallos y reintentos ante caídas de APIs externas. |
| **`python-performance-optimization`** | [`skills/python-performance-optimization/`](skills/python-performance-optimization/SKILL.md) | Profiling y optimización de memoria/concurrencia en scripts de ingesta y procesamiento de datos. |
| **`python-testing-patterns`** | [`skills/python-testing-patterns/`](skills/python-testing-patterns/SKILL.md) | Estrategias de testing con `pytest`, fixtures y mocks para validar scripts de extracción de datos. |

---

## 🚀 3. CI/CD y Automatización (GitHub Actions)

| Skill | Directorio | Descripción |
| :--- | :--- | :--- |
| **`github-actions-debugger`** | [`skills/github-actions-debugger/`](skills/github-actions-debugger/SKILL.md) | Diagnóstico rápido y depuración de flujos fallidos a partir de logs de ejecución. |
| **`github-actions-advanced`** | [`skills/github-actions-advanced/`](skills/github-actions-advanced/SKILL.md) | Hardening de workflows programados (`cron`), manejo seguro de secretos (`GEE_SERVICE_ACCOUNT_KEY`) y caching. |

---

## 🗺️ 4. Visualización Cartográfica y Frontend (Web GIS)

| Skill | Directorio | Descripción |
| :--- | :--- | :--- |
| **`command-center-ui`** | [`skills/command-center-ui/`](skills/command-center-ui/SKILL.md) | Guía de interfaz para centros de control y monitoreo, mapas operacionales globales/regionales y alertas críticas. |
| **`dashboard-design`** | [`skills/dashboard-design/`](skills/dashboard-design/SKILL.md) | Diseño analítico, jerarquía de métricas climáticas, leyendas interactivas y paneles flotantes de datos. |

---

## 🔒 5. Seguridad y Auditoría

| Skill | Directorio | Descripción |
| :--- | :--- | :--- |
| **`client-secret-exposure-audit`** | [`skills/client-secret-exposure-audit/`](skills/client-secret-exposure-audit/SKILL.md) | Auditoría para evitar la exposición accidental de API keys, tokens o credenciales privadas en el frontend web. |

---

## Uso

Todos los skills están registrados tanto en el repositorio como en el motor de Antigravity. Puedes activar cualquiera de ellos simplemente indicando la tarea en el chat (por ejemplo: *"Optimiza el script de ANACAFE usando `python-performance-optimization`"* o *"Audita el frontend con `client-secret-exposure-audit`"*).
