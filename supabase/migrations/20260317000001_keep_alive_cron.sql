-- Keep-alive cron to prevent Supabase free project from sleeping after 7 days of inactivity
-- Runs every 6 days, pings the project
CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS pg_net;

SELECT cron.schedule(
  'keep-alive',
  '0 0 */6 * *',
  $$SELECT net.http_get('https://qhiwvwlbtlevltfblxso.supabase.co/rest/v1/categories?select=category_id&limit=1', headers := '{"apikey": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InFoaXd2d2xidGxldmx0ZmJseHNvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzI3MDkxNDMsImV4cCI6MjA4ODI4NTE0M30.wsgdjkTi0xILWld5Bg46aRsSLZ-tdYBmqdbRTa1mEkk"}'::jsonb)$$
);
