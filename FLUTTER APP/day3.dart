/*
Today is about CONTROL FLOW IN CHART
Control flow allows my application to make decisions and repeat tasks dynamically. In my RAG Chat App, I will use control flow constantly, such as deciding whether to display a loading spinner, checking if
a retrieved documnet chunks are empty, or rendering a scrolling list of past chat history.

Conditional Statements (if, else if, else)

Conditions let my code execute different blocks depending on whetger a condition is true or false.
 for example
String queryStatus = 'retrieving';

if (queryStatus == 'success') {
  print('Displaying retrieved context from vector store.');
} else if (queryStatus == 'retrieving') {
  print('Searching embeddings... Please wait.');
} else {
  print('Error: Failed to fetch response.');
}

Switch statements
When I need to check a single variable against multiple discrete values, a switch statement
is cleaner ans easier to read than a long chain of if-else checks.

int httpStatusCode = 200;

switch (httpStatusCode) {
  case 200:
    print('Success: RAG pipeline responded.');
    break;
  case 401:
    print('Unauthorized: Check API keys.');
    break;
  case 500:
    print('Server Error: Python backend crashed.');
    break;
  default:
    print('Unhandled status code: $httpStatusCode');
}

Loops (for and while)
Loops allow you repeat actions or iterate through collections, such as displaying a list of
chat messages on my phone screen.

Standard For Loop: Best when One needs an explicit counter index.

List<String> cachedPrompts = ['What is LlamaIndex?', 'Explain hybrid search', 'Vector DB comparison'];

for (int i = 0; i < cachedPrompts.length; i++) {
  print('Prompt ${i + 1}: ${cachedPrompts[i]}');
}

For in Loop: A cleaner, more readable syntax to iterate directly through items in a list.
for (var prompt in cachedPrompts) {
  print('Recent query: $prompt');
}



*/


// PRACTICE EXCERCISE.

/*
Controlling flow in Dart by simulating a dynamic chat message renderer. Updating my practice file or lib/main.dart with this snippet.
*/

import 'package:flutter/material.dart';

void main() {
  // Day 3: Control Flow Example
  List<String> messages = [
    'User: How do I set up LlamaIndex?',
    'AI: LlamaIndex helps connect your data to LLMs.',
    'User: Thanks! What about vector search?'
  ];

  bool isLoading = false;

  runApp(
    MaterialApp(
      home: Scaffold(
        appBar: AppBar(title: const Text('RAG Chat - Control Flow')),
        body: Padding(
          padding: const EdgeInsets.all(16.0),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Conditional UI rendering
              if (isLoading)
                const LinearProgressIndicator()
              else
                const Text('Status: Ready for queries', style: TextStyle(color: Colors.green)),
              const SizedBox(height: 20),
              const Text('Conversation History:', style: TextStyle(fontWeight: FontWeight.bold)),
              const SizedBox(height: 10),
              
              // Loop through messages and display them
              Expanded(
                child: ListView.builder(
                  itemCount: messages.length,
                  itemBuilder: (context, index) {
                    return Padding(
                      padding: const EdgeInsets.symmetric(vertical: 4.0),
                      child: Text(messages[index]),
                    );
                  },
                ),
              ),
            ],
          ),
        ),
      ),
    ),
  );
}



