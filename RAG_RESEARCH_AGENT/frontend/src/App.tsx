import React from 'react';
import { AgentChat } from './components/AgentChat';
import { SessionSidebar } from './components/SessionSidebar';
import { useAgentChat } from './hooks/useAgentChat';

export default function App() {
  const chat = useAgentChat();

  return (
    <div style={{ display: 'flex', height: '100vh' }}>
      <SessionSidebar
        currentSessionId={chat.currentSessionId}
        onNewSession={chat.startNewSession}
        onLoadSession={chat.loadSession}
      />
      <AgentChat
        messages={chat.messages}
        isStreaming={chat.isStreaming}
        onSendMessage={chat.sendMessage}
        onNewSession={chat.startNewSession}
      />
    </div>
  );
}
