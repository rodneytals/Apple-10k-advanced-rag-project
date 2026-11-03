## Week 1, Day 5: Asynchronous Programming (`async`/`await`)

In a full-stack RAG application, your Flutter mobile app cannot freeze up every time it sends a user query to your PHP/Python backend and waits for a response. Because network requests take time, Dart uses **asynchronous programming** to perform tasks in the background while keeping your user interface completely smooth and responsive.

---

### 1. The Concept of a `Future`

In Dart, a **`Future`** represents a value that isn't available *yet*, but will be at some point in the future (like an HTTP response from your server or a file read operation).

* When you call an asynchronous operation, Dart doesn't block the main thread; instead, it hands back a `Future` and keeps running the rest of your code.
* When the operation finishes, the `Future` completes with either data or an error.

---

### 2. `async` and `await`

To work with `Future` objects cleanly without getting tangled up in callbacks, Dart uses the `async` and `await` keywords:

* **`async`**: Marks a function as asynchronous, telling Dart that it will contain delayed operations.
* **`await`**: Pauses the execution of that specific function until the `Future` finishes and returns its result, without freezing the rest of the application.

**Example: Simulating a Network Request to a RAG Backend**

```dart
// A function that simulates sending a prompt to your backend and waiting for vector search results
Future<String> fetchAIResponse(String userPrompt) async {
  print('Connecting to RAG backend...');
  
  // Simulate a 2-second network delay
  await Future.delayed(const Duration(seconds: 2));
  
  return 'AI Answer: Based on your vector database, LlamaIndex handles document chunking efficiently.';
}

```

---

### 3. Handling Asynchronous UI Data (`FutureBuilder`)

In Flutter, if you want to display data that comes from an asynchronous source (like fetching chat history or API responses), you use a widget called **`FutureBuilder`**. It automatically handles three states for you:

1. **Waiting / Loading**: While the request is in flight (show a loading spinner).
2. **Done with Data**: When the response arrives successfully (render the data).
3. **Error**: If the server crashes or times out (show an error message).

---

### 4. Day 5 Practice Exercise

Let's test an asynchronous mock API call inside a Flutter app. Replace your `lib/main.dart` code with the snippet below to see how a loading state handles an asynchronous AI query:

```dart
import 'package:flutter/material.dart';

void main() {
  runApp(const RAGAsyncApp());
}

class RAGAsyncApp extends StatelessWidget {
  const RAGAsyncApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'RAG Async Demo',
      theme: ThemeData(primarySwatch: Colors.blue),
      home: const AsyncChatScreen(),
    );
  }
}

class AsyncChatScreen extends StatefulWidget {
  const AsyncChatScreen({super.key});

  @override
  State<AsyncChatScreen> createState() => _AsyncChatScreenState();
}

class _AsyncChatScreenState extends State<AsyncChatScreen> {
  // Simulating an asynchronous call to our future Python/PHP backend
  Future<String> _queryBackend() async {
    await Future.delayed(const Duration(seconds: 3)); // simulate network latency
    return "Retrieved context: Vector embeddings successfully matched with a score of 0.89.";
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Day 5: Async & Futures')),
      body: Center(
        child: Padding(
          padding: const EdgeInsets.all(24.0),
          child: FutureBuilder<String>(
            future: _queryBackend(),
            builder: (context, snapshot) {
              // 1. While waiting for the future to complete
              if (snapshot.connectionState == ConnectionState.waiting) {
                return const Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    CircularProgressIndicator(),
                    SizedBox(height: 16),
                    Text('Querying vector store via API...'),
                  ],
                );
              } 
              // 2. If an error occurs
              else if (snapshot.hasError) {
                return Text('Error: ${snapshot.error}', style: const TextStyle(color: Colors.red));
              } 
              // 3. When data is successfully received
              else {
                return Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    const Icon(Icons.check_circle, color: Colors.green, size: 48),
                    const SizedBox(height: 16),
                    Text(
                      snapshot.data ?? 'No data received',
                      textAlign: TextAlign.center,
                      style: const TextStyle(fontSize: 16, fontWeight: FontWeight.bold),
                    ),
                  ],
                );
              }
            },
          ),
        ),
      ),
    );
  }
}

```

I should notice a loading spinner play for 3 seconds before smoothly transitioning to display the retrieved vector context!

---