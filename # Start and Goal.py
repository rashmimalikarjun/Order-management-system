# Start and Goal
start = (8, 0)
goal = (0, 0)

# Possible moves
moves = [(-1,0), (1,0), (0,-1), (0,1)]

# Heuristic function (Manhattan distance)
def h(n):
    return abs(n[0] - goal[0]) + abs(n[1] - goal[1])

# Open list: (f, g, current_node, path)
open_list = [(h(start), 0, start, [start])]
visited = set()

while open_list:
    open_list.sort()          # get node with lowest f
    f, g, current, path = open_list.pop(0)

    if current == goal:
        print("Path is:", path)
        break

    visited.add(current)

    for move in moves:
        nxt = (current[0] + move[0], current[1] + move[1])

        if 0 <= nxt[0] <= 8 and 0 <= nxt[1] <= 2 and nxt not in visited:
            open_list.append((g + 1 + h(nxt), g + 1, nxt, path + [nxt]))
