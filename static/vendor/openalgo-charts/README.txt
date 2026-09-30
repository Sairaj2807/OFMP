Prebuilt OpenAlgo Charts 2.4.0 (Apache-2.0, see LICENSE and NOTICE).

Only the two tiers the order-flow page uses are included:
  openalgo-charts.mjs          base engine (createChart, candlestick series)
  openalgo-charts.profile.mjs  Footprint primitive (self-contained, no imports)

Built from ../../../openalgo-charts-master in a scratch copy with
`npm ci && npm run build`, so the vendored source folder stays untouched.
To rebuild: repeat that and copy the two .mjs files here.
