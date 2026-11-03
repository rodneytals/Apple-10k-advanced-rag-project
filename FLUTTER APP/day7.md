## Week 1, Day 7: Interactive Chat UI & User Input

Congratulations on reaching the final day of Week 1! You have mastered Dart syntax, control flow, modular widgets, asynchronous programming, and HTTP requests.

Today, you will tie everything together by building an **interactive chat screen UI** complete with a text input field (`TextField`), a controller to capture user prompts, and a dynamic message list that updates when you type. This lays the exact foundation for your frontend before we wire it up to your PHP and Python RAG backend in Week 2.

---

### 1. Capturing Input with `TextEditingController`

In Flutter, to read what a user types into a text box, you use a **`TextEditingController`**.

* It acts as a bridge between the input field on your screen and your Dart code.
* You can use it to extract the text, clear the input box after sending, or listen for changes.

```dart
// Declare a controller inside your State class
final TextEditingController _promptController = TextEditingController();

// To read the current text typed by the user:
String userQuery = _promptController.text;

// To clear the text box after sending:
_promptController.clear();

```

---

### 2. The Chat Input Layout

A standard chat layout consists of two main parts wrapped in a `Column`:

1. **`Expanded` with a `ListView.builder**`: Displays the scrolling conversation history.
2. **A Bottom Input Bar**: A row containing a `TextField` for typing and an `IconButton` (or `ElevatedButton`) to send the message.

```dart
Padding(
  padding: const EdgeInsets.all(8.0),
  child: Row(
    children: [
      Expanded(
        child: TextField(
          controller: _promptController,
          decoration: const InputDecoration(
            hintText: 'Ask your RAG database...',
            border: OutlineInputBorder(),
          ),
        ),
      ),
      const SizedBox(width: 8),
      IconButton(
        icon: const Icon(Icons.send),
        onPressed: () {
          // Trigger message sending logic here
        },
      ),
    ],
  ),
)

```

---

### 3. Day 7 Practice Exercise: Interactive Chat Screen

Let's put this together into a complete, working interactive chat interface. When you type a message and hit send, it dynamically adds your prompt to the message list and triggers a simulated AI response.

Replace your `lib/main.dart` code with this snippet:

```dart
import 'package:flutter/material.dart';

void main() {
  runApp(const RAGChatApp());
}

class RAGChatApp extends StatelessWidget {
  const RAGChatApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'RAG Chat UI',
      theme: ThemeData(primarySwatch: Colors.blue),
      home: const ChatScreen(),
    );
  }
}

class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key});

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  // Controller to read text input
  final TextEditingController _controller = TextEditingController();

  // List holding chat messages (map containing text and sender flag)
  final List<Map<String, dynamic>> _messages = [
    {'text': 'System: RAG database loaded successfully.', 'isUser': false},
  ];

  bool _isGenerating = false;

  // Function to handle sending a prompt
  void _handleSendMessage() async {
    final text = _controller.text.trim();
    if (text.isEmpty) return;

    setState(() {
      // Add user message to list
      _messages.add({'text': text, 'isUser': true});
      _isGenerating = true;
    });

    _controller.clear();

    // Simulate backend retrieval latency (will connect to PHP/Python next week!)
    await Future.delayed(const Duration(seconds: 2));

    setState(() {
      _messages.add({
        'text': 'AI Response: Retrieved chunks for "$text" from vector store.',
        'isUser': false,
      });
      _isGenerating = false;
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('RAG Chat Companion - Week 1 Capstone')),
      body: Column(
        children: [
          // 1. Chat Message List View
          Expanded(
            child: ListView.builder(
              itemCount: _messages.length,
              itemBuilder: (context, index) {
                final msg = _messages[index];
                final isUser = msg['isUser'] as bool;
                return Container(
                  margin: const EdgeInsets.symmetric(vertical: 4.0, horizontal: 12.0),
                  alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
                  child: Container(
                    padding: const EdgeInsets.all(12.0),
                    constraints: BoxConstraints(maxWidth: MediaQuery.of(context).size.width * 0.75),
                    decoration: BoxDecoration(
                      color: isUser ? Colors.blue[700] : Colors.grey[200],
                      borderRadius: BorderRadius.circular(12.0),
                    ),
                    child: Text(
                      msg['text'],
                      style: TextStyle(color: isUser ? Colors.white : Colors.black87),
                    ),
                  ),
                );
              },
            ),
          ),

          // 2. Loading Indicator while waiting for "backend"
          if (_isGenerating)
            const Padding(
              padding: EdgeInsets.all(8.0),
              child: LinearProgressIndicator(),
            ),

          // 3. Bottom Text Input Bar
          Container(
            padding: const EdgeInsets.all(8.0),
            color: Colors.grey[100],
            child: Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: _controller,
                    decoration: const InputDecoration(
                      hintText: 'Ask a question about your documents...',
                      border: OutlineInputBorder(),
                      contentPadding: EdgeInsets.symmetric(horizontal: 12.0, vertical: 8.0),
                    ),
                    onSubmitted: (_) => _handleSendMessage(),
                  ),
                ),
                const SizedBox(width: 8.0),
                IconButton(
                  icon: const Icon(Icons.send, color: Colors.blue),
                  onPressed: _handleSendMessage,
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

```

Save the file (`Ctrl + S`), test typing a message in your app, and watch your dynamic chat interface handle real-time input and state changes!

---

### **Week 1 Complete! 🚀**

You have successfully built out the entire Flutter UI foundation for your RAG client.

Whenever you are ready, let me know, and we will step into **Week 2: Setting up Laragon, PHP, and MySQL for your backend API!**