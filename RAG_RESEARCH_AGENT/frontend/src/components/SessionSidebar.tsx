import React, { useState, useEffect, useCallback } from 'react';
import { fetchAuthSession } from 'aws-amplify/auth';
import { getConfig } from '../config/runtime';

interface Session { session_id: string; created_at: string; preview: string; }
interface Props { currentSessionId: string; onNewSession: () => void; onLoadSession: (sid: string) => void; }

export function SessionSidebar({ currentSessionId, onNewSession, onLoadSession }: Props) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const config = getConfig();

  const loadSessions = useCallback(async () => {
    try {
      const auth = await fetchAuthSession();
      const token = auth.tokens?.idToken?.toString() || '';
      const resp = await fetch(`${config.api.rest_endpoint}agent/sessions`, {
        headers: { 'Authorization': `Bearer ${token}` },
      });
      const data = await resp.json();
      setSessions(data.sessions || []);
    } catch (err) {
      console.error('Failed to load sessions:', err);
    }
  }, [config.api.rest_endpoint]);

  useEffect(() => { loadSessions(); }, [loadSessions]);

  return (
    <aside style={{ width: 280, borderRight: '1px solid #e0e0e0', display: 'flex', flexDirection: 'column', backgroundColor: '#fafafa' }}>
      <div style={{ padding: 16, borderBottom: '1px solid #e0e0e0' }}>
        <button onClick={onNewSession} style={{ width: '100%', padding: 10, borderRadius: 8 }}>
          + New Research Session
        </button>
      </div>
      <nav style={{ flex: 1, overflowY: 'auto', padding: 8 }} aria-label="Session history">
        {sessions.map(s => (
          <button key={s.session_id} onClick={() => onLoadSession(s.session_id)}
            style={{ display: 'block', width: '100%', textAlign: 'left', padding: 10, marginBottom: 4,
              borderRadius: 6, border: 'none', cursor: 'pointer',
              backgroundColor: s.session_id === currentSessionId ? '#e3f2fd' : 'transparent' }}
            aria-current={s.session_id === currentSessionId ? 'true' : undefined}>
            <div style={{ fontSize: 13, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {s.preview || 'New session'}
            </div>
            <div style={{ fontSize: 11, color: '#888', marginTop: 2 }}>{s.created_at?.split('T')[0]}</div>
          </button>
        ))}
      </nav>
    </aside>
  );
}
