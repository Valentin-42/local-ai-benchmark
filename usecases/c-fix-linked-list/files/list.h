#ifndef LIST_H
#define LIST_H

typedef struct Node {
    int value;
    struct Node *prev;
    struct Node *next;
} Node;

typedef struct {
    Node *head;
    Node *tail;
    int size;
} List;

void list_init(List *l);
void list_push_back(List *l, int value);
/* Removes the first node holding `value`. Returns 1 if removed, 0 if not found. */
int list_remove(List *l, int value);
void list_print(const List *l);
void list_free(List *l);

#endif
