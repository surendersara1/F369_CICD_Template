/**
 * React hook for Strands agent chat — WebSocket streaming with REST fallback.
 */
import { useState, useEffect, useRef, useCallback } from 'react';
import { fetchAuthSession } from 'aws-amplify/auth';
import { getConfig } from '../config/runtime';

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
}

const CONFIG = getConfig();

export function useAgentChat(sessionId?: string) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [currentSessionId, setCurrentSessionId] = useState(sessionId || crypto.randomUUID());
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>();

  const connectWS = useCallback(async () => {
    if (!CONFIG.features.streaming_enabled || !CONFIG.api.ws_endpoint) return;
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString() || '';
      const url = `${CONFIG.api.ws_endpoint}?token=${token}&session_id=${currentSessionId}`;
      const ws = new WebSocket(url);

      ws.onopen = () => console.log('[AgentChat] WebSocket connected');
      ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'message') {
          setMessages(prev => [...prev, { role: 'assistant', content: data.content, timestamp: data.timestamp }]);
          setIsStreaming(false);
        } else if (data.type === 'status' && data.status === 'thinking') {
          setIsStreaming(true);
        } else if (data.type === 'status' && data.status === 'done') {
          setIsStreaming(false);
        }
      };
      ws.onclose = () => { reconnectTimer.current = setTimeout(connectWS, 3000); };
      ws.onerror = (err) => console.error('[AgentChat] WS error:', err);
      wsRef.current = ws;
    } catch (err) {
      console.error('[AgentChat] Failed to connect:', err);
    }
  }, [currentSessionId]);

  useEffect(() => {
    connectWS();
    return () => { clearTimeout(reconnectTimer.current); wsRef.current?.close(); };
  }, [connectWS]);

  const sendMessage = useCallback(async (content: string) => {
    const userMsg: ChatMessage = { role: 'user', content, timestamp: new Date().toISOString() };
    setMessages(prev => [...prev, userMsg]);

    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action: 'message', message: content, session_id: currentSessionId }));
    } else {
      setIsStreaming(true);
      try {
        const session = await fetchAuthSession();
        const token = session.tokens?.idToken?.toString() || '';
        const resp = await fetch(`${CONFIG.api.rest_endpoint}agent/invoke`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
          body: JSON.stringify({ message: content, session_id: currentSessionId }),
        });
        const data = await resp.json();
        setMessages(prev => [...prev, { role: 'assistant', content: data.response, timestamp: new Date().toISOString() }]);
      } catch (err) {
        setMessages(prev => [...prev, { role: 'assistant', content: 'Something went wrong. Please try again.', timestamp: new Date().toISOString() }]);
      } finally {
        setIsStreaming(false);
      }
    }
  }, [currentSessionId]);

  const startNewSession = useCallback(() => { setMessages([]); setCurrentSessionId(crypto.randomUUID()); }, []);

  const loadSession = useCallback(async (sid: string) => {
    try {
      const session = await fetchAuthSession();
      const token = session.tokens?.idToken?.toString() || '';
      const resp = await fetch(`${CONFIG.api.rest_endpoint}agent/sessions/${sid}`, {
        headers: { 'Authorization': `Bearer ${token}` },
      });
      const data = await resp.json();
      const loaded: ChatMessage[] = data.turns.flatMap((t: any) => [
        { role: 'user' as const, content: t.user_message, timestamp: t.created_at },
        { role: 'assistant' as const, content: t.agent_response, timestamp: t.created_at },
      ]);
      setMessages(loaded);
      setCurrentSessionId(sid);
    } catch (err) {
      console.error('[AgentChat] Failed to load session:', err);
    }
  }, []);

  return { messages, sendMessage, isStreaming, currentSessionId, startNewSession, loadSession };
}
