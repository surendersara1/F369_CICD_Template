/**
 * Runtime configuration — injected at deploy time via SSM parameter.
 * In development, falls back to environment variables.
 */
export interface RuntimeConfig {
  cognito: {
    user_pool_id: string;
    app_client_id: string;
    region: string;
  };
  api: {
    rest_endpoint: string;
    ws_endpoint: string;
  };
  features: {
    streaming_enabled: boolean;
    session_history_enabled: boolean;
    file_upload_enabled: boolean;
  };
}

export function getConfig(): RuntimeConfig {
  const windowConfig = (window as any).__RUNTIME_CONFIG__;
  if (windowConfig) return windowConfig;

  return {
    cognito: {
      user_pool_id: process.env.REACT_APP_USER_POOL_ID || '',
      app_client_id: process.env.REACT_APP_CLIENT_ID || '',
      region: process.env.REACT_APP_REGION || 'us-east-1',
    },
    api: {
      rest_endpoint: process.env.REACT_APP_API_URL || 'http://localhost:3001/',
      ws_endpoint: process.env.REACT_APP_WS_URL || '',
    },
    features: {
      streaming_enabled: true,
      session_history_enabled: true,
      file_upload_enabled: true,
    },
  };
}
