#include "glasgow.h"

/*

Should only work here and not touch the API stuff. 

Inputs are dag and target. 

Helpful note: for the post method, the current mapping and routing are stored in dag, which is nice

*/

void glasgow_mapping(QkDag *dag, QkTarget *target, subgraph_vec mapping){

    printf("HELLO FROM C...glasgow\n");

    return NULL;

}

void glasgow_mapping_post(QkDag *dag, QkTarget *target, subgraph_vec mapping){

    printf("HELLO FROM C...glasgow_post\n");

    return NULL;

}