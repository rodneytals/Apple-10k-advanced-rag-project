## Day 6: HTTP Networking & API Integration in Flutter

Now that I understand asynchronous programming and `Future` objects, it is time to connect your Flutter frontend to the real world. In a full-stack architecture, your mobile app acts as the client that sends user prompts and receives vector search outputs from your backend server.

Today, I will learn how to use Dart’s `http` package to send requests and handle JSON data payloads.

---

### 1. Adding the HTTP Package

To make network requests in Flutter, you use the official `http` package.

1. Open your `pubspec.yaml` file in the root of your project.
2. Under `dependencies:`, add the `http` package:
```yaml
dependencies:
  flutter:
    sdk: flutter
  http: ^1.2.0 # Allows sending GET and POST requests

```


3. Run `flutter pub get` in your terminal to download and install the package.

---

### 2. Making GET and POST Requests

Because network communication happens over the internet (or local server), all HTTP calls must be marked as `async` and use `await`.

* **GET Request (Fetching data):**
```dart
import 'http:http.dart' as http;
import 'dart:convert'; // Needed for jsonDecode

Future<void> fetchServerStatus() async {
  final url = Uri.parse('http://10.0.2.2/rag_api/status.php'); // 10.0.2.2 points to localhost from an Android emulator
  try {
    final response = await http.get(url);
    if (response.statusCode == 200) {
      print('Server response: ${response.body}');
    } else {
      print('Failed with status: ${response.statusCode}');
    }
  } catch (e) {
    print('Network error: $e');
  }
}

```


* **POST Request (Sending user prompts to your RAG backend):**
When your user types a question in the chat, you package it into a JSON map and send it via a POST request:
```dart
Future<void> sendUserPrompt(String promptText) async {
  final url = Uri.parse('http://10.0.2.2/rag_api/query.php');

  try {
    final response = await http.post(
      url,
      headers: {'Content-Type': 'application/json; charset=UTF-8'},
      body: jsonEncode({
        'prompt': promptText,
        'top_k': 5,
      }),
    );

    if (response.statusCode == 200) {
      final Map<String, dynamic> data = jsonDecode(response.body);
      print('AI Result: ${data['answer']}');
    }
  } catch (e) {
    print('Error sending prompt: $e');
  }
}

```



---

### 3. Day 6 Practice Exercise

Let's test an asynchronous HTTP request structure inside Flutter using a public test API (`jsonplaceholder.typicode.com`). This simulates fetching initial chat history data from a server before you spin up your local PHP backend.

Replace your `lib/main.dart` code with this snippet:

```dart
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'dart:convert';

void main() {
  runApp(const NetworkApp());
}

class NetworkApp extends StatelessWidget {
  const NetworkApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Day 6: HTTP Networking',
      theme: ThemeData(primarySwatch: Colors.blue),
      home: const NetworkScreen(),
    );
  }
}

class NetworkScreen extends StatefulWidget {
  const NetworkScreen({super.key});

  @override
  State<NetworkScreen> createState() => _NetworkScreenState();
}

class _NetworkScreenState extends State<NetworkScreen> {
  String _serverData = 'Press the button to fetch server data...';
  bool _isLoading = false;

  // Function to perform an HTTP GET request
  Future<void> _fetchDataFromApi() async {
    setState(() {
      _isLoading = true;
    });

    try {
      // Using a public JSON placeholder API for testing network connectivity
      final url = Uri.parse('https://jsonplaceholder.typicode.com/posts/1');
      final response = await http.get(url);

      if (response.statusCode == 200) {
        final Map<String, dynamic> data = jsonDecode(response.body);
        setState(() {
          _serverData = 'Server Title: "${data['title']}"';
          _isLoading = false;
        });
      } else {
        setState(() {
          _serverData = 'Error: Server returned status ${response.statusCode}';
          _isLoading = false;
        });
      }
    } catch (e) {
      setState(() {
        _serverData = 'Connection Failed: Check network or emulator settings.';
        _isLoading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Day 6: API Integration')),
      body: Padding(
        padding: const EdgeInsets.all(24.0),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Text(
              _serverData,
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 18),
            ),
            const SizedBox(height: 30),
            _isLoading
                ? const CircularProgressIndicator()
                : ElevatedButton.onPressed(
                    onPressed: _fetchDataFromApi,
                    child: const Text('Fetch Server Response'),
                  ),
          ],
        ),
      ),
    );
  }
}

```

---