// PM2 ecosystem config for Principal Miner Rewards.
// Usage: pm2 start ecosystem.config.js

module.exports = {
  apps: [
    {
      name: "principal-miner-api",
      script: "uvicorn",
      args: "app.main:app --host 0.0.0.0 --port 8100",
      interpreter: "python3",
      cwd: __dirname,
      env_file: ".env",
      max_restarts: 10,
      restart_delay: 5000,
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
    },
    {
      name: "principal-miner-epoch-monitor",
      script: "python3",
      args: "-m app.jobs.epoch_monitor",
      cwd: __dirname,
      env_file: ".env",
      max_restarts: 50,
      restart_delay: 10000,
      kill_timeout: 30000,  // 30s grace period (BT chain calls can be slow)
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
    },
  ],
};
