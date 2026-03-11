import React, { useState, useRef, useEffect } from 'react';
import { ChatMessage } from '../hooks/useAgentChat';
import { SourceCitationCard } from './SourceCitationCard';
import { FileUpload } from './FileUpload';

interface Props {
  messages: ChatMessage[];
  isStreaming: boolean;
  onSendMessage: (content: string) => void;
  onNewSession: () => void;
}

export function AgentChat({ messages, isStreaming, onSendMessage, onNewSession }: Props) {
  const [input, setInput] = useState('');
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isStreaming) return;
    onSendMessage(input.trim());
    setInput('');
  };

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
      <header style={{ padding: 16, borderBottom: '1px solid #e0e0e0', display: 'flex', justifyContent: 'space-between' }}>
        <h2 style={{ margin: 0 }}>RAG Research Agent</h2>
        <div style={{ display: 'flex', gap: 8 }}>
          <FileUpload />
          <button onClick={onNewSession} aria-label="Start new conversation">New Chat</button>
        </div>
      </header>
      <main style={{ flex: 1, overflowY: 'auto', padding: 16 }} role="log" aria-live="polite">
        {messages.map((msg, i) => (
          <div key={i} style={{ marginBottom: 12, padding: 12, borderRadius: 8,
            backgroundColor: msg.role === 'user' ? '#e3f2fd' : '#f5f5f5',
            maxWidth: '80%', marginLeft: msg.role === 'user' ? 'auto' : 0 }}
            role="article" aria-label={`${msg.role === 'user' ? 'You' : 'Agent'}`}>
            <strong>{msg.role === 'user' ? 'You' : 'Research Agent'}</strong>
            <div style={{ marginTop: 4, whiteSpace: 'pre-wrap' }}>{msg.content}</div>
            {msg.role === 'assistant' && <SourceCitationCard content={msg.content} />}
          </div>
        ))}
        {isStreaming && <div style={{ padding: 12, color: '#666' }} aria-live="assertive"><em>Researching...</em></div>}
        <div ref={endRef} />
      </main>
      <form onSubmit={handleSubmit} style={{ padding: 16, borderTop: '1px solid #e0e0e0', display: 'flex', gap: 8 }}>
        <label htmlFor="chat-input" className="sr-only">Type your message</label>
        <input id="chat-input" type="text" value={input} onChange={e => setInput(e.target.value)}
          placeholder="Ask a research question..." disabled={isStreaming}
          style={{ flex: 1, padding: 12, borderRadius: 8, border: '1px solid #ccc' }} />
        <button type="submit" disabled={isStreaming || !input.trim()}
          style={{ padding: '12px 24px', borderRadius: 8 }}>Send</button>
      </form>
    </div>
  );
}
