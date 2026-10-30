#include <stdio.h>
#include <stdlib.h>
#include "list.h"

void list_init(List *l) {
    l->head = NULL;
    l->tail = NULL;
    l->size = 0;
}

void list_push_back(List *l, int value) {
    Node *n = malloc(sizeof *n);
    n->value = value;
    n->next = NULL;
    n->prev = l->tail;
    if (l->tail)
        l->tail->next = n;
    else
        l->head = n;
    l->tail = n;
    l->size++;
}

int list_remove(List *l, int value) {
    Node *n = l->head;
    while (n && n->value != value)
        n = n->next;
    if (!n)
        return 0;

    n->prev->next = n->next;
    n->next->prev = n->prev;
    if (n == l->head)
        l->head = n->next;
    if (n == l->tail)
        l->tail = n->prev;
    free(n);
    l->size--;
    return 1;
}

void list_print(const List *l) {
    printf("[");
    for (const Node *n = l->head; n; n = n->next)
        printf(n->next ? "%d, " : "%d", n->value);
    printf("] (size %d)\n", l->size);
}

void list_free(List *l) {
    Node *n = l->head;
    while (n) {
        Node *next = n->next;
        free(n);
        n = next;
    }
    list_init(l);
}
