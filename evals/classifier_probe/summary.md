| Brazo | Llamadas (fallidas) | Reseñas etiquetadas | Tokens de entrada por reseña | Caché leído | Salida por reseña | Costo por reseña | Proyección 484,189 textos | Latencia media | Acuerdo con Haiku (topic / intent / severidad) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| haiku_cold | 1 (0) | 50 | 63 | 0 | 17 | $0.0000158 | $7.67 (lote: $3.83) | 4124 ms | — |
| haiku_warm | 1 (0) | 50 | 63 | 34 | 17 | $0.0000120 | $5.79 (lote: $2.89) | 3492 ms | — |
| jev_single | 50 (0) | 50 | 904 | 0 | 202 | $0.0000380 | $18.39 | 183 ms | 88% / 96% / 80% |
| jev_packed_10 | 1 (0) | 10 | 350 | 0 | 205 | $0.0000147 | $7.12 | 743 ms | 70% / 100% / 80% |
| jev_packed_25 | 1 (0) | 25 | 305 | 0 | 207 | $0.0000128 | $6.20 | 453 ms | 76% / 92% / 72% |
| jev_packed_50 | 1 (0) | 50 | 296 | 0 | 208 | $0.0000124 | $6.01 | 609 ms | 82% / 98% / 74% |
| decisions_single | 50 (50) | 0 | no medido: RateLimitError: Error code: 429 - {'error': {'message': 'You have no credits remaining. Ad | | | | | | |
| decisions_packed_10 | 1 (1) | 0 | no medido: RateLimitError: Error code: 429 - {'error': {'message': 'You have no credits remaining. Ad | | | | | | |
| decisions_packed_50 | 1 (1) | 0 | no medido: RateLimitError: Error code: 429 - {'error': {'message': 'You have no credits remaining. Ad | | | | | | |

Empaque contra individual (topic / intent / severidad): Jev 50 vs Jev individual 92% / 94% / 86%; Decisions 50 vs Decisions individual —; Jev individual vs Decisions individual —.

Caché oculto, cache_decisions: no medido (RateLimitError: Error code: 429 - {'error': {'message': 'You have no credits remaining. Ad).
Caché oculto, cache_jev: tokens de entrada por repetición [886, 886, 886], leídos de caché [0, 0, 0].

Gasto total de la prueba: $0.0045 (tarifas de `cost/rates.csv`, uso reportado por cada API).
