
#include <cstring>
#include <iostream>
#include <fstream>
#include <unistd.h>
using namespace std;

// g++ -o dummyengine dummyengine.cpp

int main(int argc, char *argv[])
{
    printf("Dummy engine output\n");

    int i;

    for (i = 1; i < argc; i++) {
        FILE* f = fopen(argv[i], "r");
        printf("processing arg as a file: %s\n", argv[i]);

        fseek(f, 0, SEEK_END);
        size_t size = ftell(f);

        char* content = new char[size];
        char* outfname = new char[256];

        rewind(f);
        fread(content, sizeof(char), size, f);

        strcat(outfname, argv[i]);
        strcat(outfname, ".out");

        printf("filename to be used: %s\n", outfname);

        ofstream output(outfname, ios::out | ios::binary);
        output.write(content, size);
        output.close();
        delete[] content;
        delete[] outfname;
    }

sleep(4);

return 0;
}