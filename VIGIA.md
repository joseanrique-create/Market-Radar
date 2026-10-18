# Vigía de mercado

Monitor automático de estrés financiero. Corre entero en la nube: **no depende
de ningún computador encendido.**

## Cómo funciona

```
GitHub Actions (cron, 22:00 UTC L-V)
   │
   ├─► vigia.py  ──► Yahoo Finance        (cierres casi en tiempo real)
   │                 └─ respaldo: FRED vía Cloudflare Worker
   │
   ├─► compara con estado.json (versionado en el repo)
   │
   └─► ¿algún indicador cambió de color?
          sí ──► abre un issue  ──► GitHub te manda el correo
          no ──► no hace nada
```

El principio es **avisar solo cuando algo cambia**. Si todo sigue igual no
llega nada, así que un correo del vigía siempre significa algo.

## Los ocho indicadores

| Indicador | Verde | Amarillo | Rojo | Qué mide |
|---|---|---|---|---|
| S&P 500 vs máximo 52s | > -5% | -5 a -10% | < -10% | Caída efectiva del mercado |
| S&P 500 vs media 200d | sobre | 0 a -5% | < -5% | Tendencia de largo plazo |
| Nasdaq vs máximo 52s | > -5% | -5 a -10% | < -10% | Dónde golpea primero |
| VIX | < 20 | 20-30 | > 30 | Volatilidad esperada |
| Estructura VIX3M − VIX | > 0 | 0 a -1,5 | < -1,5 | Backwardation = estrés agudo |
| Spread high yield | < 350 pb | 350-500 | > 500 | **Si el crédito se tranca** |
| Curva 10a − 2a | ≥ 0 | 0 a -0,3 | < -0,3 | Señal de recesión anticipada |
| Regla de Sahm | < 0,3 | 0,3-0,5 | > 0,5 | Deterioro real del empleo |

El más importante es el **spread high yield**. Mejor que el VIX para distinguir
una corrección de una crisis: mide si las empresas todavía pueden financiarse.
Una caída de bolsa con crédito fluido es una rotación. Una caída con el spread
sobre 500 pb es otra cosa.

## Lo que el vigía NO hace

No predice caídas, porque no se puede. Y porque además no serviría:

- Un cambio de fondo en la AFP demora **4 días hábiles**.
- El tope normativo limita los cambios de un mismo día al **5% del patrimonio**
  del fondo y los procesa por antigüedad, así que en pánico demora más.
- Para cuando la plata se mueve, la caída ya pasó.

Por eso el vigía sirve para otra cosa: **ejecutar reglas escritas en frío.**
El valor no está en adivinar el crash, está en no improvisar cuando llegue.

## Fecha límite real

El **1 de abril de 2027** los multifondos A–E desaparecen y los reemplazan diez
fondos generacionales:

- **Ahorro obligatorio:** queda asignado por año de nacimiento, sin opción de
  elegir otro fondo. Quien quiera moverse solo puede cambiar de AFP.
- **Ahorro voluntario (APV, Cuenta 2):** sí se puede elegir, y hay que hacerlo
  **antes de fines de marzo de 2027** o queda asignado por edad por defecto.

Conviene verificar los detalles en la Superintendencia de Pensiones
(Resolución Exenta N.º 1195, del 1-sep-2026).

## Operación

| Acción | Cómo |
|---|---|
| Ver el estado ahora | [Dashboard](https://joseanrique-create.github.io/Market-Radar/) |
| Correr a mano | Actions → *Vigia de mercado* → *Run workflow* |
| Forzar un aviso | Igual, con *forzar_aviso* en `true` |
| Ver el historial | `git log estado.json` |
| Cambiar umbrales | Las funciones `r_*()` en `vigia.py` |
| Cambiar horario | El `cron` en `.github/workflows/vigia.yml` |

Sin secretos configurados funciona igual, porque usa el Worker. Si algún día
quieres saltarte el Worker, agrega `FRED_API_KEY` en Settings → Secrets and
variables → Actions, y el script la usa directo.

---
No constituye asesoría financiera.
