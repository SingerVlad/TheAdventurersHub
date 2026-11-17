# Auditoría y endurecimiento de seguridad

Auditoría de código realizada el 16 de noviembre de 2026. Este registro describe controles implementados y trabajo pendiente; no es una certificación de seguridad ni una autorización para manejar datos reales.

## Controles implementados

- Configuración de producción falla al iniciar sin una clave secreta de al menos 32 bytes, una URI de MongoDB explícita y orígenes CORS HTTPS.
- CORS usa una lista explícita de orígenes. Las respuestas incluyen encabezados de protección y no se cachean.
- Los JWT requieren emisor, expiración, fecha de emisión, usuario y nonce CSRF; vencen en 30 minutos. El navegador usa una cookie `HttpOnly`, `Secure` en producción y `SameSite` configurable. Las escrituras autenticadas desde cookie requieren un encabezado CSRF; el token no se guarda en `localStorage`.
- Registro, login, generación de NPC, generación de imágenes y unión a campañas tienen límites compartidos entre instancias almacenados en MongoDB. Los contadores expiran mediante TTL.
- Registro valida nombre y longitud de contraseña, y crea un índice único de usuario. Los errores de API no devuelven trazas ni excepciones internas.
- El endpoint de diagnóstico de proveedores requiere autenticación.
- La autorización de campaña se comprueba en el backend para assets vinculados y acceso a imágenes. El servidor ya no expone una ruta pública para explorar la carpeta de Foundry.
- Las imágenes guardadas tienen límite de tamaño, firma PNG, nombre generado por el servidor y ruta derivada de IDs internos; las rutas se resuelven dentro de la raíz permitida.
- CI ejecuta comprobaciones de seguridad unitarias junto con sintaxis, lint, build y Conventional Commits.

## Antes de producción

1. **Migración de usuarios:** comprobar duplicados de `usuarios.username` y crear el índice único en una operación de mantenimiento antes del despliegue. El registro devuelve `503` mientras no pueda verificar ese índice.
2. **Sesiones:** añadir revocación de sesiones/tokens y una política de rotación. Cerrar sesión borra la cookie del navegador; un token robado aún puede usarse hasta su expiración.
3. **Abuso y costos:** definir límites por plan y presupuestos de proveedor para IA. El límite actual es una defensa por ventana, no un sistema de cuotas comerciales ni detección de abuso.
4. **Proxy de producción:** configurar TLS y establecer `TRUST_PROXY_COUNT` según la cantidad exacta de proxies confiables. Compose lo establece en `1` detrás de Nginx; el backend no publica su puerto.
5. **Archivos:** migrar el almacenamiento local a almacenamiento de objetos privado y controlado, con política de retención, antivirus/decodificación segura y URLs firmadas o entrega autorizada. La firma PNG y el límite de tamaño no sustituyen una decodificación y recodificación de imagen.
6. **Ciclo de vida de cuentas:** implementar verificación de correo, recuperación segura de contraseña, cierre/eliminación de cuenta, exportación y retención/eliminación de campañas.
7. **Operación:** usar secretos gestionados, MongoDB con TLS y privilegio mínimo, backups cifrados y probados, monitorización de autenticación/abuso y proceso de respuesta a incidentes.
8. **Validación:** ampliar pruebas con MongoDB real o réplica de pruebas, pruebas de acceso cruzado entre usuarios y campañas, pruebas de cookies/CORS en el dominio de producción, y análisis de dependencias.

No habilites datos reales ni facturación hasta completar estas tareas y revisar la configuración de infraestructura.
