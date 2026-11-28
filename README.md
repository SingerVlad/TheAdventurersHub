# ⚔️ The Adventurer's Hub

Plataforma web integral para la gestión de campañas de juegos de rol (D&D 5e), generación asistida por Inteligencia Artificial de NPCs y retratos, e integración directa de recursos con **Foundry VTT**.

---

## 🏗️ Arquitectura y Tecnologías

### Backend (`/`)
- **Framework:** Python 3.12 + Flask (`app.py`)
- **Base de Datos:** MongoDB (`flask-pymongo` / `pymongo`) — Base de datos: `adventurers_hub`
- **Autenticación:** JWT (`PyJWT`) + hash de contraseñas con `Werkzeug`
- **Motores de IA:**
  - **Texto (NPCs y optimización de prompts):** Groq API (`llama-3.1-8b-instant`)
  - **Imágenes (Retratos):** Hugging Face Inference API (`black-forest-labs/FLUX.1-schnell`)
  - **Diagnóstico / Auxiliar:** Google GenAI SDK (`google-genai`)
- **Integración Foundry VTT:** Almacenamiento y sincronización física de retratos en `FOUNDRY_DATA_PATH`.

### Frontend (`/frontend`)
- **Framework:** React 19 + Vite 7
- **Estilos:** Tailwind CSS v4 + Lucide React (iconografía)
- **Estado Global:** Zustand (`src/store/authStore.js`)
- **Cliente HTTP:** Axios (`src/api.js`)

---

## 🗄️ Estructura de Colecciones (MongoDB)

| Colección | Descripción | Campos Principales |
| :--- | :--- | :--- |
| `usuarios` | Cuentas de jugadores y Dungeon Masters | `_id`, `username`, `password_hash`, `fecha_registro` |
| `campanas` | Partidas creadas y sus miembros | `_id`, `nombre`, `descripcion`, `codigo_invitacion`, `dm_id`, `jugadores` (array de `ObjectId`) |
| `assets` | NPCs (JSON) y Retratos (archivos PNG) | `_id`, `usuario_id`, `campana_id`, `tipo` (`NPC` \| `Retrato`), `nombre`, `prompt_original`, `file_path`, `estado_aprobacion`, `fecha_creacion` |
| `logs_generacion` | Auditoría de llamadas a motores de IA | `_id`, `usuario_id`, `prompt_original`, `art_prompt`, `fecha`, `engine_texto`, `engine_imagen` |

---

## 🚀 Instalación y Ejecución Local

### Ejecutar todo con Docker Compose

Requisitos: Docker Desktop con Docker Compose v2.

Si todavía no tienes `.env`, crea uno a partir del ejemplo. En PowerShell:

```powershell
Copy-Item .env.example .env
```

Si `.env` ya existe, no lo sobrescribas: agrega o ajusta `MONGO_ROOT_USERNAME`, `MONGO_ROOT_PASSWORD`, `MONGO_APP_USERNAME` y `MONGO_APP_PASSWORD`. Configura también `SECRET_KEY` si aún está vacío. Usa los nombres `adventurers_root` y `adventurers_app`; genera las contraseñas con este comando:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "import secrets; print(secrets.token_hex(32))"
```

Usa el primer comando para `SECRET_KEY` y el segundo para ambas contraseñas MongoDB. Las contraseñas Mongo deben mantenerse hexadecimales para que la URI de conexión no requiera escape adicional. Luego inicia la plataforma:

```bash
docker compose up --build -d
```

Abre [http://localhost:8080](http://localhost:8080). La API y MongoDB solo son accesibles dentro de la red privada de Compose; MongoDB conserva los datos en el volumen `mongo_data` y los retratos en `foundry_assets`.

Comandos útiles:

```bash
docker compose ps
docker compose logs -f backend frontend mongo
docker compose down
```

`docker compose down` conserva los volúmenes. `docker compose down -v` también elimina permanentemente la base de datos y los retratos. No ejecutes esta última instrucción si necesitas conservar datos.

Foundry VTT continúa ejecutándose fuera de Compose; configura `VITE_FOUNDRY_URL` para que el navegador pueda abrir esa instalación. Para hacer la web accesible desde otros equipos, define `WEB_BIND_ADDRESS=0.0.0.0` y limita el acceso con el firewall. Este Compose está preparado para desarrollo local. En producción se requiere terminar TLS en un proxy y establecer `APP_ENV=production`, `COOKIE_SECURE=true`, `TRUST_PROXY_COUNT` con el número exacto de proxies y orígenes HTTPS en `CORS_ORIGINS`; gestiona las credenciales con el almacén de secretos de la plataforma.

### 1. Requisitos Previos
- **Python 3.12**
- **Node.js 20+** y **npm**
- **MongoDB** ejecutándose localmente (`mongodb://localhost:27017`)

### 2. Configurar Backend
```bash
# Crear y activar entorno virtual
python -m venv .venv
# En Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
# En Linux/macOS:
source .venv/bin/activate

# Instalar dependencias
pip install -r requirements.txt

# Configurar variables de entorno
cp .env.example .env
# Genera un SECRET_KEY privado: python -c "import secrets; print(secrets.token_urlsafe(48))"
# Configura las claves de proveedores solo si utilizarás esas integraciones.

# Iniciar servidor Flask (puerto 5000)
python app.py
```

### 3. Configurar Frontend
```bash
cd frontend
npm install

# (Opcional) Configurar variables de entorno locales
cp .env.example .env

# Iniciar servidor de desarrollo (puerto 5173)
npm run dev
```

---

## 🔌 Endpoints Principales de la API

| Método | Ruta | Auth | Descripción |
| :--- | :--- | :---: | :--- |
| `POST` | `/api/registro` | ❌ | Registro de nuevo usuario |
| `POST` | `/api/login` | ❌ | Inicio de sesión con JWT en cookie `HttpOnly` (30 minutos) |
| `GET` | `/api/perfil` | ✅ | Datos del usuario autenticado |
| `GET` | `/api/campanas` | ✅ | Lista campañas donde el usuario es DM o Jugador |
| `POST` | `/api/campanas` | ✅ | Crea una campaña y genera `codigo_invitacion` (8 caracteres) |
| `POST` | `/api/campanas/unirse` | ✅ | Une al usuario a una campaña mediante código |
| `GET` | `/api/campanas/<campana_id>` | ✅ | Detalle de campaña, miembros y assets vinculados |
| `POST` | `/api/ia/npc-rapido` | ✅ | Genera un NPC estructurado (JSON) con Groq (`llama-3.1-8b-instant`) |
| `POST` | `/api/ia/generar-imagen` | ✅ | Optimiza prompt con Groq y genera retrato Base64 con FLUX.1-schnell |
| `GET` | `/api/assets` | ✅ | Obtiene los assets (NPCs y Retratos) del usuario |
| `POST` | `/api/assets` | ✅ | Guarda un asset de texto/JSON (NPC) |
| `POST` | `/api/assets/guardar-imagen` | ✅ | Guarda retrato físico en `FOUNDRY_DATA_PATH` y lo vincula en MongoDB |
| `DELETE` | `/api/assets/<asset_id>` | ✅ | Elimina asset de MongoDB y del disco físico |

---

## 🔀 Flujo de Trabajo con Ramas y Pull Requests (`develop` / `main`)

1. **Ramas principales:**
   - `main`: Código estable de producción.
   - `develop`: Rama base de desarrollo e integración. **Todos los cambios nuevos nacen desde `develop`**.
2. **Crear una rama descriptiva siempre a partir de `develop`:**
   ```bash
   git checkout develop
   git pull origin develop
   git checkout -b feat/nombre-funcionalidad
   ```
   - `feat/nombre-funcionalidad` (nuevas características)
   - `fix/nombre-bug` (corrección de errores)
   - `docs/nombre-cambio` (documentación o contexto)
   - `refactor/nombre-cambio` (mejoras de código sin cambiar comportamiento)
3. **Verificación antes de abrir PR:**
   - Asegurar que no se suban archivos `.env`, `.venv/` ni bases de datos locales.
   - Ejecutar `npm run lint` y `npm run build` dentro de `frontend/`.
4. **Abrir Pull Request en GitHub hacia `develop`** completando la plantilla automática y esperar a que pasen las validaciones de GitHub Actions (`CI`).

## Estado técnico

Consulta la [matriz del estado real de main](docs/estado-main.md) para ver el código integrado, los PR en revisión y las pruebas pendientes.
