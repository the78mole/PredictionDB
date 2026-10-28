COPY (
  SELECT ts, code, value
    FROM energy.value_best
   WHERE ts >= '2020-01-01'
     AND code IN ('da_price_de_lu', 'load_actual', 'load_da_forecast', 'residual_load',
                  'residual_load_da_forecast', 'gen_wind_onshore', 'gen_wind_offshore', 'gen_solar',
                  'gen_da_forecast_wind_onshore', 'gen_da_forecast_wind_offshore',
                  'gen_da_forecast_solar', 'gen_da_forecast_total', 'gen_hydro_pumped', 'gen_gas',
                  'gen_lignite', 'gen_hard_coal')
   ORDER BY code, ts
) TO STDOUT WITH (FORMAT csv, HEADER);
