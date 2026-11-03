/*
## Week 1, Day 4: Functions and Modular Code

As my application grows, writing all your code inside the main function or a single file becomes unmanageable. Today, you will learn how to write reusable functions and split your Flutter UI into modular widgets—a crucial skill for building clean, scalable architectures like your upcoming RAG chat application.

---

### 1. Dart Functions & Parameters

Functions allow you to encapsulate logic so you can reuse it across your app. Dart supports both positional parameters and **named parameters**, which are heavily used in Flutter UI development.

* **Standard Function (Arrow Syntax):**
For short, single-line operations, you can use the fat arrow (`=>`) syntax to keep your code clean:
```dart
// A helper function to format query strings before sending them to Python
String cleanQuery(String rawInput) => rawInput.trim().toLowerCase();

```


* **Named Parameters (Flutter Style):**
Using curly braces `{}` makes parameters optional or required, and allows you to pass them by name, preventing argument-order bugs:
```dart
// Function with named parameters
String buildApiPayload({required String prompt, int topK = 5, bool useHybridSearch = true}) {
  return 'Query: "$prompt" | Top-K: $topK | Hybrid: $useHybridSearch';
}

// Calling it:
// buildApiPayload(prompt: 'What is LlamaIndex?');

```



---

### 2. Modularizing Flutter Code (Extracting Custom Widgets)

In Flutter, everything is a widget. Instead of nesting thousands of lines inside `main.dart`, you break complex screens down into smaller, focused stateless or stateful widgets.

For example, instead of writing raw chat bubble layouts inline, you can create a dedicated `ChatMessageCard` widget:

```dart
class ChatMessageCard extends StatelessWidget {
  final String text;
  final bool isUser;

  // Constructor requiring the parameters
  const ChatMessageCard({super.key, required this.text, required this.isUser});

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.symmetric(vertical: 4.0, horizontal: 8.0),
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        padding: const EdgeInsets.all(12.0),
        decoration: BoxDecoration(
          color: isUser ? Colors.blue[100] : Colors.grey[200],
          borderRadius: BorderRadius.circular(8.0),
        ),
        child: Text(text),
      ),
    );
  }
}

```

---

### 3. Day 4 Practice Exercise

Let's integrate functions and modular widgets into a clean script. Update your `lib/main.dart` with this modularized chat structure:

```dart
import 'package:flutter/material.dart';

void main() {
  runApp(const RAGChatApp());
}

// 1. Root App Widget
class RAGChatApp extends StatelessWidget {
  const RAGChatApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'RAG Chat Companion',
      theme: ThemeData(primarySwatch: Colors.blue),
      home: const ChatScreen(),
    );
  }
}

// 2. Chat Screen State Container
class ChatScreen extends StatelessWidget {
  const ChatScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final List<Map<String, dynamic>> mockMessages = [
      {'text': 'How do I query my vector database?', 'isUser': true},
      {'text': 'You can use LlamaIndex retriever engines with LangChain agents.', 'isUser': false},
    ];

    return Scaffold(
      appBar: AppBar(title: const Text('Modular RAG Chat')),
      body: ListView.builder(
        itemCount: mockMessages.length,
        itemBuilder: (context, index) {
          final msg = mockMessages[index];
          return ChatMessageCard(
            text: msg['text'],
            isUser: msg['isUser'],
          );
        },
      ),
    );
  }
}

// 3. Reusable Modular Component Widget
class ChatMessageCard extends StatelessWidget {
  final String text;
  final bool isUser;

  const ChatMessageCard({super.key, required this.text, required this.isUser});

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.symmetric(vertical: 6.0, horizontal: 12.0),
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        padding: const EdgeInsets.all(12.0),
        constraints: BoxConstraints(maxWidth: MediaQuery.of(context).size.width * 0.75),
        decoration: BoxDecoration(
          color: isUser ? Colors.blue[700] : Colors.grey[300],
          borderRadius: BorderRadius.circular(12.0),
        ),
        child: Text(
          text,
          style: TextStyle(color: isUser ? Colors.white : Colors.black87),
        ),
      ),
    );
  }
}

```

Save the file and watch your device render a clean, modular chat architecture!

---

Would you like to move on to **Week 1, Day 5: Asynchronous Programming (`async`/`await` and HTTP API calls)** next?
*/