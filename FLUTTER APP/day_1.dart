/* 
# Day 1 notes.
## The core variable types
String: Text data wrapped in quotes (e.g., 'Hello world').
int: Whole numbers (e.r., 42).
double: Floating-point decimal numbers (e.g., 3.14).
bool: True or false boolean flags (true or false).

## Dart's Null Safety (? and !)
Dart enforces Sound Null Safety by default. This means variables cannot be null unless I explicitly tell Dart they are allowed to be empty.

*/

/*// Non-Nullable (Default):
String username = 'Rodney'; // can never be set to null

void main() {

  // Nullable(?):
  // Adding ? tells Dart the variable might hold a value OR might be null.
  String? botResponse; // Can be null initially until the AI replies

  // Nul-Check Operator(!):
  // Tells Dart 'I guarantee this nullable variable is not null right now, Trust me.'
  print(botResponse!.length);

  // Null aware Operator(??):
  //provides a default fallback if something is null.
  String displayMessage = botResponse ?? 'Ai is thinking ...';

}*/

// Excercise, i shall be replacing the code of main.dart in the root folder with the code below.

import 'package:flutter/material.dart';

void main() {
  // Day 2: Variables & Null Safety
  String appName = 'RAG Chat Companion';
  int messgeCount = 0;
  double apiLatency = 120.5;
  bool isServerOnline = true;

  // Nullabale variable example
  String? lastUserPrompt;

  runApp(
    MaterialApp(
      home: Scaffold(
        appBar: AppBar(title: Text(appName)),
        body: Center(
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Text('Status: ${isServerOnline ? "Connected" : "Offline"}'),
              Text('Messages Sent: $messgeCount'),
              Text('Latency: ${apiLatency}ms'),
              const SizedBox(height: 20),
              Text('Last Prompt: ${lastUserPrompt ?? "No prompts sent yet"}'),
            ],
          ),
        ),
      ),
    ),
  );
}

