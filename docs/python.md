# python

## list
lst = [1, 2, 3]                           # Create a list
lst.append(4)                             # Add an item to the end
lst.insert(0, "x")                        # Insert an item at an index
lst.pop()                                 # Remove and return the last item
lst.pop(0)                                # Remove and return item at an index
lst.remove(2)                             # Remove the first matching value
lst[1:3]                                  # Slice (index 1 up to, not including, 3)
lst[::-1]                                 # Reverse a list via slicing
lst.sort()                                # Sort in place
sorted(lst)                               # Return a new sorted list (original unchanged)
lst.reverse()                             # Reverse in place
len(lst)                                  # Number of items
x in lst                                  # Membership test
[x * 2 for x in lst]                      # List comprehension
[x for x in lst if x > 0]                 # List comprehension with a filter

## tuple
t = (1, 2, 3)                             # Create a tuple (immutable)
t = 1, 2, 3                               # Parentheses are optional
t = (1,)                                  # Single-item tuple needs a trailing comma
a, b, c = t                               # Unpack a tuple
t[0]                                      # Access by index
t + (4, 5)                                # Concatenate tuples
t.count(2)                                # Count occurrences of a value
t.index(2)                                # Find the index of the first match

## dict
d = {"a": 1, "b": 2}                      # Create a dict
d["a"]                                    # Access a value by key (raises if missing)
d.get("c", 0)                             # Access with a default if the key is missing
d["c"] = 3                                # Add or update a key
d.pop("a")                                # Remove a key and return its value
d.setdefault("d", 0)                      # Get a key, inserting a default if missing
d.keys()                                  # View of all keys
d.values()                                # View of all values
d.items()                                 # View of (key, value) pairs
{k: v * 2 for k, v in d.items()}          # Dict comprehension
{**d1, **d2}                              # Merge two dicts into a new one

## set
s = {1, 2, 3}                             # Create a set (unique, unordered)
s.add(4)                                  # Add an item
s.remove(2)                               # Remove an item (raises if missing)
s.discard(2)                              # Remove an item (no error if missing)
s1 | s2                                   # Union
s1 & s2                                   # Intersection
s1 - s2                                   # Difference
{x for x in range(5)}                     # Set comprehension

## string
s = "hello world"                         # Create a string
s.upper()                                 # Uppercase
s.lower()                                 # Lowercase
s.strip()                                 # Remove leading/trailing whitespace
s.split(" ")                              # Split into a list
"-".join(["a", "b"])                      # Join a list into a string
s.replace("world", "there")               # Replace a substring
f"{name} is {age}"                        # f-string formatting
s[::-1]                                   # Reverse a string via slicing
s.startswith("he")                        # Check a prefix
s.endswith("ld")                          # Check a suffix
s.format(name="Sam")                      # Old-style string formatting

## function
def add(a, b=0):                          # Define a function with a default argument
def greet(*args, **kwargs):               # Accept variable positional/keyword arguments
lambda x: x * 2                           # Anonymous one-line function
map(str, lst)                             # Apply a function to every item
filter(lambda x: x > 0, lst)              # Keep only items matching a condition
sum(lst)                                  # Sum a list of numbers
max(lst)                                  # Largest item
min(lst)                                  # Smallest item

## loop & control
for x in lst:                             # Iterate over a list
for i, x in enumerate(lst):               # Iterate with an index
for k, v in d.items():                    # Iterate over dict key/value pairs
while condition:                          # Loop while a condition holds
break                                     # Exit the nearest loop early
continue                                  # Skip to the next loop iteration
zip(list1, list2)                         # Pair up items from two iterables
range(0, 10, 2)                           # Numbers 0, 2, 4, 6, 8

## exception
try:                                      # Start a try block
except ValueError as e:                   # Catch a specific exception type
except Exception:                         # Catch any exception (broad, use sparingly)
finally:                                  # Always runs, whether or not an error occurred
raise ValueError("message")               # Raise an exception

## file
open("file.txt")                          # Open a file for reading (text mode)
with open("file.txt") as f:               # Open a file safely (auto-closes)
f.read()                                  # Read the entire file as one string
f.readlines()                             # Read the file as a list of lines
open("file.txt", "w")                     # Open a file for writing (overwrites)
open("file.txt", "a")                     # Open a file for appending
