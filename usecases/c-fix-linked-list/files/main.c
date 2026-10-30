#include <stdio.h>
#include "list.h"

int main(void) {
    List l;
    list_init(&l);
    for (int i = 1; i <= 5; i++)
        list_push_back(&l, i);
    list_print(&l);

    list_remove(&l, 3);     /* middle node */
    list_print(&l);

    list_remove(&l, 5);     /* last node */
    list_print(&l);

    list_remove(&l, 1);     /* first node */
    list_print(&l);

    list_free(&l);
    return 0;
}
