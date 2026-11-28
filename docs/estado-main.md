# Estado técnico de main

Referencia: `b6293ad` de `SingerVlad/TheAdventurersHub`, revisado el 27 de noviembre de 2026.

| Diseño del informe 2 | Implementado en main | Pendiente o en revisión |
| --- | --- | --- |
| Registro, sesión y campañas | React consume la API Flask. El backend genera códigos de invitación de 8 caracteres y acepta códigos alfanuméricos de 6 a 8 caracteres. | El formulario de main limita la entrada a 6 caracteres. El [PR #3](https://github.com/SingerVlad/TheAdventurersHub/pull/3) corrige el formulario y prepara el índice único de invitaciones al arrancar. |
| Galería y borrado persistente | La API permite al propietario borrar su recurso. El botón de main solo lo quita de la lista en pantalla. | El PR #3 conecta el botón con la API. Se verificó con un NPC ficticio en Docker Compose que el borrado persiste al recargar. |
| Moderación anti-spoiler | Los recursos nuevos se guardan como aprobados en el main revisado. El detalle muestra los recursos vinculados a los miembros de la campaña. | El [PR #4](https://github.com/SingerVlad/TheAdventurersHub/pull/4) incorpora los estados Pendiente, Aprobado y Rechazado. Se probaron los tres estados en navegador con un DM y un jugador. Falta validar Foundry VTT. |
| Datos e índices | MongoDB guarda usuarios, campañas, recursos, registros de generación y contadores de solicitudes. Hay un índice único de usuario y un índice TTL para los contadores. | El índice de invitaciones está en el PR #3. Las mediciones y los índices adicionales se registran en las tareas de datos. |
| Instalación e integración de retratos | Compose ejecuta el frontend con Nginx, el backend Flask con Gunicorn y MongoDB. Los retratos se guardan en un volumen interno. | En Windows se verificaron los tres servicios saludables y la persistencia de una campaña. Falta repetir la instalación en otro equipo y comprobar el acceso desde Foundry. |
| Generación con IA | Groq genera texto, Hugging Face genera retratos y Google GenAI se usa para diagnóstico. | Los flujos dependen de las claves y la disponibilidad de los proveedores. Queda registrar sus pruebas en el entorno de entrega. |

## Sesión y permisos

- El inicio de sesión entrega un JWT en la cookie `HttpOnly` `access_token`, con vigencia de 30 minutos. Las solicitudes que modifican datos usando esa cookie requieren `X-CSRF-Token`. El frontend recupera el valor con `GET /api/csrf`.
- La lista y el detalle de campañas se limitan al DM y sus jugadores. El borrado exige que la cuenta sea propietaria del recurso.
- `GET /api/assets/<asset_id>/file` exige autenticación. En el main revisado, el archivo personal es accesible al propietario y el de campaña a sus miembros. El filtro de descarga por aprobación corresponde al PR #4.
- La ruta interna usa el ID de campaña o el directorio `personal`, el ID del propietario y un nombre PNG aleatorio. El frontend solicita la imagen mediante el ID del recurso.

## Datos y operación

`usuarios` guarda las cuentas. `campanas` referencia al DM y a sus jugadores por ID. `assets` referencia al propietario y, cuando corresponde, a una campaña. Los retratos guardan una ruta relativa. `logs_generacion` registra la generación de imágenes y `rate_limits` mantiene contadores con expiración.

La imagen del backend y CI usan Python 3.12. CI ejecuta `python -m unittest discover -s tests`, `npm run lint` y `npm run build`. Los pasos de arranque están en el [README](../README.md).

Si Compose indica que faltan variables, revisa `SECRET_KEY`, `MONGO_ROOT_PASSWORD` y `MONGO_APP_PASSWORD` en el `.env` local. Si un servicio no queda saludable, usa `docker compose ps` y `docker compose logs --tail 100 backend frontend mongo`. Mantén las claves fuera de Git y de las capturas.
