# Frontend integration

D1 publishes its Vite build into apps/web/dist. The Compose proxy serves that directory and forwards /api/v1 to the Python API using the same origin. CASECHAMP_WEB_DIST can point to a different build location.
