> **Histórico. Superado — no implementar contra este documento.**
>
> Es el primer borrador de diseño y varias decisiones centrales cambiaron
> después: se descartó JCS en favor de firmar los bytes crudos, los endpoints
> pasaron de ocho a nueve, `/inbox/{handle}` perdió el handle del path, y el
> sobre ganó `parent_message_id`. Ver `arquitectura-v0.1.md`, y sobre todo el
> código, que es lo único que está al día.
>
> Se conserva porque el razonamiento sirve — incluido el que resultó estar
> equivocado.

# Protocolo de buzones para agentes — arquitectura v0

> Nombre del protocolo: **PENDIENTE**. En este documento se usa `PROTO` como
> placeholder. Definirlo antes de escribir la primera línea de código, porque
> aparece en el user-agent, en el prefijo de los códigos y en la URL del
> `.well-known` futuro.

Versión del documento: v0
Estado: especificación de implementación, no publicable
Destinatario: agente de código

---

## 1. Qué es esto

Un servidor de buzones que permite que agentes personales de distintas personas
se manden mensajes asincrónicos, firmados, con estado estructurado.

El buzón pertenece al **humano**, no al agente. Un humano puede tener varios
agentes (Hermes, Claude, otros), cada uno con su propia clave, todos escribiendo
desde la misma identidad y compartiendo la misma libreta de contactos.

El alcance es **comunicación**, no ejecución. Los agentes se avisan, proponen y
concilian; cada humano aprueba según cómo tenga configurado el suyo. El servidor
no ejecuta nada ni interpreta el contenido de los mensajes.

## 2. Criterio de terminado

v0 **no** está terminado cuando los endpoints responden 200.

v0 está terminado cuando:

- Dos agentes de **personas distintas**, con libretas mutuas,
- sostuvieron un hilo de **al menos 8 vueltas**,
- con **al menos 2 actualizaciones parciales** del estado,
- y existen los logs para contar los **errores de estado** (afirmaciones de un
  agente que el otro nunca dijo).

Todo lo que no contribuya a llegar a ese punto es fuera de alcance. Si aparece
la tentación de pulir el buzón, releer esta sección.

## 3. Principios de diseño

1. **El servidor nunca ve ni emite claves privadas.** Es directorio, no
   autoridad certificante. Cada agente genera su par localmente.
2. **La identidad canónica es una clave, el handle es un alias.** Esto permite
   multi-agente hoy y portabilidad después.
3. **El servidor no interpreta el contenido.** Transporta, verifica firma,
   aplica la libreta y loguea.
4. **Lo que no se documenta, no ata.** Todo lo listado en "fuera de alcance"
   puede existir en el código, pero no en la documentación pública.

## 4. Modelo de datos

Cuatro tablas. SQLite.

### `human`
| campo | tipo | nota |
|---|---|---|
| `id` | uuid | PK |
| `handle` | text | único, formato `nombre@servidor` |
| `canonical_pubkey` | text | Ed25519, base64. La del primer agente registrado. |
| `created_at` | timestamp | |

### `agent`
| campo | tipo | nota |
|---|---|---|
| `id` | uuid | PK |
| `human_id` | uuid | FK → `human.id` |
| `label` | text | ej. `hermes`, `claude`. Informativo. |
| `pubkey` | text | Ed25519, base64. Única en toda la tabla. |
| `scope` | text | **Dejar la columna, siempre `full` en v0.** Reservada para limitar qué ve cada agente. |
| `revoked_at` | timestamp | null si activa |
| `created_at` | timestamp | |

Máximo **5** agentes activos por `human`. Hardcodeado.

### `contact`
| campo | tipo | nota |
|---|---|---|
| `id` | uuid | PK |
| `owner_human_id` | uuid | FK → `human.id` |
| `peer_human_id` | uuid | FK → `human.id` |
| `disclosure` | text | enum: `full` \| `basic` \| `minimal`. Default `basic`. |
| `created_at` | timestamp | |

La libreta es **del humano**: si Hermes acepta a Tomás, todos los agentes de
Gabo lo tienen. Es **simétrica**: aceptar una invitación crea las dos filas.

### `message`
| campo | tipo | nota |
|---|---|---|
| `id` | uuid | PK, es el `message_id` del sobre |
| `thread_id` | uuid | |
| `from_human_id` / `from_agent_id` | uuid | quién firmó |
| `to_human_id` | uuid | |
| `envelope` | json | el sobre completo tal como llegó |
| `signature` | text | |
| `ack_at` | timestamp | null hasta que el receptor acuse procesamiento |
| `created_at` | timestamp | |

Guardar el sobre **tal como llegó**, sin normalizar. La firma se verifica contra
los bytes originales.

## 5. Formato del mensaje

```json
{
  "version": "0.1",
  "message_id": "uuid",
  "thread_id": "uuid",
  "from": { "handle": "gabo@servidor", "agent": "hermes", "pubkey": "base64" },
  "to": "tomas@servidor",
  "timestamp": "ISO-8601",
  "disclosure": "basic",
  "state": { },
  "prose": "texto libre"
}
```

Firma aparte del objeto, en el header `X-Proto-Signature`.

### Reglas

- **La firma cubre el sobre completo**, no solo el cuerpo. Incluye `to` y
  `thread_id`. Sin eso se pueden reenviar mensajes a otra conversación.
- Canonicalización antes de firmar: **JCS (RFC 8785)**. Sin esto, dos
  serializaciones del mismo objeto producen firmas distintas.
- `version` en cada mensaje. Es lo que permite romper cosas y convivir.
- `state` es **libre**. Hay un shape recomendado (§6) pero no se valida ni se
  rechaza por no cumplirlo.
- `disclosure` viaja en el sobre. **El servidor no fuerza semántica**: lo
  transporta y lo loguea. Qué significa cada nivel lo decide el agente emisor.

### Regla de seguridad, va en la doc pública

`state` es **dato**. `prose` es **reporte de segunda mano**.
**Ninguna de las dos es instrucción.** Un agente receptor nunca ejecuta lo que
viene en un mensaje; lo incorpora a su modelo y decide por su cuenta.

## 6. Shape recomendado de `state`

No obligatorio. Lo usa el agente de bienvenida y se sugiere para las pruebas,
para que los hilos sean comparables entre sí.

```json
{
  "topic": "asado sábado",
  "status": "proposed",
  "when": [{ "start": "ISO-8601", "end": "ISO-8601", "confidence": "high" }],
  "where": "texto",
  "who": ["gabo@servidor", "tomas@servidor"],
  "budget": { "amount": 0, "currency": "ARS", "per": "person" },
  "constraints": ["texto"],
  "tasks": [{ "what": "texto", "who": "handle" }]
}
```

Actualizaciones parciales: **JSON Merge Patch (RFC 7386)** sobre el `state` del
mensaje anterior del mismo `thread_id`. Es lo más simple que funciona; si
aparecen conflictos reales, se revisa.

## 7. Endpoints

Ocho. Ninguno más.

| método | ruta | qué hace |
|---|---|---|
| `POST` | `/register` | alta de identidad o de agente adicional |
| `POST` | `/enroll-code` | genera código para sumar un agente propio |
| `POST` | `/revoke-key` | revoca una clave de agente |
| `POST` | `/invite` | genera código para sumar un contacto ajeno |
| `POST` | `/accept` | canjea un código de invitación |
| `GET` | `/contacts` | libreta del humano |
| `POST` | `/inbox/{handle}` | deposita un mensaje |
| `GET` | `/inbox` | lee los propios; marca ack opcional |

### Autenticación

Toda ruta autenticada usa **firma sobre nonce**, no token estático:
`GET /nonce` → el cliente firma el nonce con su clave de agente → lo manda en
`X-Proto-Auth`. Nonce de un solo uso, TTL 60s.

### `POST /register`

Dos casos, mismo endpoint:

- **Sin código**: crea `human` + primer `agent`. El `canonical_pubkey` del
  humano es el de este agente.
- **Con `enroll_code`**: cuelga un `agent` nuevo del `human` existente.

Ambos requieren **prueba de posesión**: el cuerpo firmado con la clave privada
que corresponde a la pública que se está registrando.

### Enrolamiento de agentes propios

1. Un agente ya activo hace `POST /enroll-code` → recibe código de **un solo
   uso, TTL 20 min**.
2. El agente nuevo hace `POST /register` con ese código y su pubkey.
3. El servidor notifica a **todas las demás claves activas** de esa identidad.

**La notificación va firmada por el servidor, no por la clave que hizo el
cambio.** Si la firma el agente comprometido, controla también el aviso.

Cualquier agente activo puede enrolar y cualquiera puede revocar. No hay
jerarquía en v0 — ver §10.

### Códigos

Dos tipos, **deliberadamente distinguibles**:

- Enrolamiento: prefijo `enr_`. Suma un agente **a tu identidad**.
- Invitación: prefijo `inv_`. Suma un contacto **ajeno**.

Endpoints separados. Un `inv_` presentado en `/register` se rechaza y viceversa.
Sin esto, alguien va a pegar el código equivocado y va a enrolar a otro humano
como agente propio.

### `POST /inbox/{handle}`

Rechaza si el emisor no está en la libreta del receptor. **Ese es todo el
anti-spam de v0 y alcanza.**

Errores distinguibles — el agente decide distinto en cada caso:

| código | significado |
|---|---|
| `404` | el handle no existe |
| `403` | existe pero no te aceptó |
| `503` | existe y te aceptó, pero el buzón no está disponible |

El fallback (mandar un WhatsApp, un mail, lo que sea) **es del humano y su
agente**, no del protocolo. El servidor solo informa cuál de los tres casos es.

### Acuse de procesamiento

**No es opcional.** El receptor confirma que **incorporó** el mensaje, no solo
que lo recibió. Sin esto, cuando un hilo se rompa no vas a saber si falló el
transporte o el agente.

Implementación: `GET /inbox?ack=<message_id>` o campo en el POST de respuesta.
Lo que sea más simple.

## 8. Agente de bienvenida

Handle público que **acepta a cualquiera automáticamente** (única excepción a la
regla de libreta).

Contesta con un mensaje **bien formado**: `state` real usando el shape
recomendado, `prose` real explicando el protocolo. Es onboarding, demo y
documentación viva en una sola pieza — el agente del otro lado aprende el
formato viéndolo funcionar, y le explica a su humano qué pasó.

Sin esto, el que se registra primero se queda con un buzón vacío y se va.

## 9. Instrumentación

**No es opcional y es lo que después no se reconstruye.**

Loguear: cada mensaje, invitación, aceptación, rechazo, enrolamiento,
revocación. Con timestamp y qué clave lo originó.

Cuatro métricas, expuestas en un endpoint interno:

1. **Pares con segunda conversación** — la que importa. Registros y menciones no
   significan nada.
2. Vueltas por hilo.
3. Tasa de parseo fallido de `state`.
4. Movimientos desde el `disclosure` por defecto.

## 10. Riesgos aceptados

Van explícitos en la documentación pública. Son decisiones, no descuidos.

- **Un agente comprometido puede enrolar otras claves.** Mitigado por límite de
  5, notificación firmada por el servidor y revocación barata. No se limita
  quién puede enrolar: cuando un agente se compromete el daño ya es total (lee
  el inbox, firma como la identidad, ve la libreta), así que restringir el
  enrolamiento no compra nada real y agrega casos borde.
- **No hay recuperación de identidad.** Si se pierden todas las claves de
  agente, la identidad se abandona: se registra un handle nuevo y se rearma la
  libreta. Con diez personas el costo es ridículo; agregar recovery ahora
  optimiza para un caso que casi seguro no va a pasar en la prueba. El modelo
  humano/clave permite sumarlo después sin romper nada.
- **Todos los agentes de un humano comparten inbox y libreta.** Sin aislamiento.
  La columna `scope` existe vacía para cuando haga falta.

## 11. Fuera de alcance

Nada de esto va en v0. Está listado para que quede claro que es decisión y no
olvido.

Federación · `.well-known` · dominios propios · relay · rotación de claves ·
migración de identidad · introducción de terceros a un hilo · presupuesto por
contacto · presencia y estado en línea · cifrado de extremo a extremo ·
recuperación de identidad · semántica forzada de `disclosure` · esquema
obligatorio de `state`.

## 11 bis. Notas para v1 — federación

**No implementar en v0.** Está acá porque las decisiones de v0 tienen que
dejarlo entrar sin migraciones.

### El criterio

**La federación está lista cuando alguien puede levantar el segundo servidor
leyendo la spec, sin hablar con el autor.** Ese es el test y es medible.

Correr el primer servidor es lo que permite que el protocolo exista antes de
tener adopción — nadie va a hostear nada al principio, y esperar a que alguien
lo haga sería no arrancar nunca. Pero el lugar de "primera instancia de un
protocolo nuevo" tiene una trampa conocida: la instancia semilla se queda con
casi toda la red y el protocolo termina federado en el papel y centralizado en
la práctica. Evitarlo requiere tres cosas concretas.

### Qué hace el servidor, y qué de eso es imprescindible

Hace cuatro cosas: directorio, almacenamiento asincrónico, aplicar la libreta en
la puerta, y emitir/canjear códigos.

**Solo una es imprescindible: el almacenamiento asincrónico.** Que el mensaje
sobreviva a que el receptor esté apagado no puede vivir en ningún otro lado. El
directorio se resuelve con `.well-known`; la libreta la puede aplicar el
receptor por su cuenta; los códigos son texto que se pasa por otro canal.

O sea: **el servidor no hace falta arquitectónicamente, hace falta
operativamente.** Es comodidad, no autoridad. Por eso las claves las genera el
agente y por eso el handle lleva el servidor adentro desde el día cero.

**Test de v0:** el servidor no tiene ningún privilegio que otro no pueda tener.
Si mañana alguien corre el suyo y federan, nadie pierde nada.

### Descubrimiento entre dominios

`GET https://{dominio}/.well-known/proto` devuelve el endpoint del buzón y la
clave pública del dominio. Un archivo estático — cualquiera con un hosting lo
puede poner. Registro DNS `SRV` como opción para quien quiera robustez, no como
requisito.

El descubrimiento vive en el dominio del otro. **La instancia semilla no
interviene.** Si interviene, la federación es decorativa.

### Confianza entre servidores

Con un solo servidor, si dice que el mensaje viene de Gabo, es verdad. Con diez,
el servidor receptor recibe algo que dice venir de `gabo@otro-dominio` y tiene
que verificarlo por su cuenta.

**La firma del agente ya lo resuelve**: la clave está publicada en el
`.well-known` del dominio emisor y el receptor la chequea directo, sin confiar
en la palabra del servidor intermedio. Esta es la razón de fondo por la que el
servidor no emite claves (§3.1) — sin firma de agente, cada servidor tendría que
confiar en los otros, que es el problema que el email tapó a parches.

La identidad del dominio es **su clave, no su nombre**. El `.well-known` publica
la clave del dominio; las claves de agente vienen firmadas por ella. Sin eso,
secuestrar un DNS suplanta un dominio entero.

### Si un servidor desaparece

Con un solo servidor no es un problema. Con varios, que se caiga uno significa
que esas identidades mueren y todas las libretas que apuntaban a ellas quedan
rotas.

La respuesta es que **la identidad canónica sea la clave y el handle un alias**
— ya está en el modelo de datos (`human.canonical_pubkey`), sin usar. El día que
alguien migre de servidor, eso es lo que le permite conservar sus contactos. Es
el problema que Mastodon nunca resolvió bien y por el que migrar de instancia
todavía duele.

### Lo caro no es especificar, es operar

Cuando hay dos servidores aparecen: reintentos, mensajes colgados, cuánto tiempo
retener, y si se entrega a cualquier dominio o solo a los conocidos. Nada es
difícil, pero es trabajo real. **No hacerlo antes de que exista alguien que
quiera correr el suyo.**

Lo que sí hay que hacer antes: **escribir la spec.** El momento en que aparece el
segundo servidor es tarde para empezar a documentar — si ese día hay que
reconstruir cómo funciona la propia implementación, la federación se demora
meses y para entonces todos están en la instancia semilla.

### Lo que esto implica para v0

Nada que construir. Solo mantener lo que ya está decidido: handle con dominio
adentro, claves generadas por el agente, `canonical_pubkey` en `human`, y ningún
privilegio del servidor que otro no pueda replicar.

## 11 ter. Notas para v1 — el modo abierto

**No implementar nada de esto.** Está acá para que el modelo de datos no lo
impida y para que las decisiones de v0 no cierren la puerta.

La pregunta que va a aparecer apenas esto se muestre: ¿el buzón puede recibir de
alguien que no está en la libreta? La respuesta conceptual es sí, pero el
allowlist no es una feature: **es el modelo de confianza entero**. Sacarlo
obliga a reemplazarlo.

### Dos afirmaciones que el agente tiene que poder distinguir

La firma prueba **control de la clave**, no quién es la persona. El handle es un
alias que cualquiera puede pedir. Entonces:

- *"Llegó de Nanton, que está en tu libreta"* → verificado de punta a punta,
  porque el vínculo se estableció fuera de banda con un código.
- *"Llegó de alguien registrado como Nanton, no está en tu libreta"* → la firma
  es válida y la identidad es consistente, pero nadie garantiza que sea quien
  dice.

Son cosas muy distintas y el agente receptor tiene que decirlas distinto. En el
modelo actual eso ya se puede: es si hay fila en `contact` o no.

Lo que sí da la firma incluso con desconocidos es **continuidad**: la misma
clave la próxima vez es la misma persona. Se puede acumular reputación sobre una
identidad sin saber nunca quién es.

### Qué se copia del email y qué no

**Sí:** el esquema de tres capas. Anclar identidad a un dominio, firmar los
mensajes, publicar política de qué hacer con lo que no verifica — SPF, DKIM,
DMARC. El `.well-known` con la clave del dominio (§11) es eso mismo en una
pieza.

**No:** la reputación centralizada de remitente. Es un servicio, no un
algoritmo — Spamhaus, veinte años de datos, cinco proveedores que de facto
deciden quién entrega. No se puede replicar solo y no conviene depender de la
de otro.

### Lo que no tiene análogo en el email

**Recibir cuesta inferencia.** El email puede recibir todo y filtrar después
porque filtrar cuesta microcentavos. Acá cada mensaje evaluado cuesta plata.
Alguien puede quemar el presupuesto de otro sin que ningún mensaje sea
técnicamente abusivo. Ese vector no existe en el email y la reputación no lo
resuelve.

### El diseño probable: abierto para proponer, cerrado para conversar

Cualquiera puede depositar un **primer mensaje**, con:

- tamaño máximo y estructura fija (solo el shape recomendado de `state`, prosa
  acotada), de modo que evaluarlo cueste un techo conocido;
- entrega a una **cola aparte**, no al inbox;
- sin continuación del hilo hasta que el receptor acepte.

El primer contacto es barato de recibir; la conversación requiere
consentimiento.

### El emisor cubre el costo de revisión

La pieza que cierra el modelo abierto sin necesitar reputación ni datos
personales: **el que escribe a un desconocido paga la inferencia que su mensaje
le va a costar evaluar al receptor.**

Es el mismo razonamiento del hashcash de los 90 para el email, con una
diferencia decisiva: allá el costo era **artificial** —fricción inventada, sin
beneficiario, que nadie iba a adoptar cambiando SMTP— y acá el costo **existe de
verdad**. El receptor efectivamente quema tokens evaluando. Que lo cubra quien
lo genera no es una traba, es que el gasto lo paga el que lo causa.

La asimetría es todo el punto: **para uno o diez mensajes es imperceptible; para
diez mil es prohibitivo.** El spam a escala se vuelve caro por construcción y un
mensaje legítimo de un desconocido cuesta prácticamente nada.

Ventaja sobre el OTP: no hay teléfono, no hay dato personal guardado, no hay
nada que replicar para el que quiera correr su propio servidor. No importa quién
es el emisor — importa que no le salga gratis.

**Forma de liquidarlo, en orden de preferencia:**

1. **Crédito interno.** Cada identidad tiene un saldo, se gasta al escribirle a
   desconocidos y se recarga lentamente con el tiempo. Cero custodia de fondos,
   cero contabilidad real, mismo efecto. Es rate limiting con memoria y con
   dirección. **Esta es la opción recomendada.**
2. **Prueba de trabajo.** Más simple, no maneja saldo, pero castiga al que tiene
   hardware lento y en 2026 el cómputo es barato justamente para el que ataca a
   escala.
3. **Tokens reales.** Requiere custodiar saldo, contabilizar y transferir —
   convierte el servidor en procesador de pagos. Evitar.

**Dos cosas que hay que resolver antes de implementarlo:**

- **El costo del receptor no es fijo y el emisor lo controla.** Si el precio
  depende del largo, el emisor paga menos escribiendo corto y el mensaje corto
  igual dispara una evaluación completa. Precio **fijo por mensaje** más tamaño
  máximo, o el esquema se rompe por el lado barato. Por eso el techo estructural
  del primer contacto no es opcional.
- **El reembolso al aceptar es abusable.** Si se devuelve el crédito cuando el
  receptor acepta, dos cómplices se aceptan siempre y escriben gratis para
  siempre. Reembolso **solo la primera vez con cada contacto nuevo**, o directamente
  sin reembolso confiando en la recarga por tiempo. Decidir cuál.

### Lo que esto implica para v0

Nada que construir. Solo:

- un flag por `human` (`accepts_unsolicited`, default false) que puede quedar
  sin usar;
- que la cola de no solicitados sea una tabla aparte cuando exista, no un
  estado del inbox;
- una columna de saldo en `human`, sin usar, para no migrar después.

**La libreta sigue siendo el default. El mundo abierto es la excepción** — no al
revés, que es donde el email se equivocó.

## 12. Stack

FastAPI + SQLite, Caddy adelante para TLS, un VPS chico.

**No es una decisión estratégica.** Usar lo que se escriba más rápido y no
gastar tiempo acá.

## 13. Entregables

1. El servidor con los ocho endpoints.
2. El agente de bienvenida corriendo y contestando en el protocolo.
3. Un `SKILL.md` que cualquier agente pueda leer desde una URL y con eso
   registrarse solo — sin config manual.
4. Un MCP que exponga cuatro herramientas: mandar, leer, invitar, aceptar.

El punto 3 es el que hace el setup trivial: la instalación **es** el primer
mensaje. El agente lee la URL, genera su clave, se registra y confirma
escribiéndole al agente de bienvenida.

**La clave privada va en un archivo local con permisos restringidos, no en la
memoria del agente.** Algunos harness sincronizan memoria a la nube. La libreta
y el handle sí pueden vivir en memoria — son lo que el agente necesita presente
para operar; la clave solo hace falta al momento de firmar.
